# Agent 上下文文档 — 标准速递 v1.0.0

> 本文档供 AI 助手理解项目全貌。用户文档见 `README.md`。
> 如果你是 Trae Solo 或其他 AI 编程助手，以下是你需要知道的全部。

---

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 运行（桌面模式）
python main.py

# 运行（纯服务模式，无需 pywebview）
python -m uvicorn app.server:app --host 127.0.0.1 --port 8000

# 运行（CLI 扫描模式）
python -m app.scanner.quick --pages=10
python -m app.scanner.quick --scan-hb=AQ,XF --incr
python -m app.scanner.quick --scan-db --incr

# 验证脚本语法
python verify_scripts.py
```

**核心入口**：`python main.py` 启动 pywebview 桌面窗口，内嵌 FastAPI 服务在 127.0.0.1:8000。

---

## 项目概述

基于 `std.samr.gov.cn` 的国家标准(GB)、行业标准(HB)、地方标准(DB) 批量扫描下载工具。应用名：**标准速递**。

**用户**：江苏无锡安全生产经理，关注市场监管、氨制冷、特种设备、冷链领域。
**核心需求**：自动扫描 → 安全关键词匹配 → 下载 PDF → 预览合成。

### 国标(GB)完整下载链路（2026-06实测）

> **⚠️ 关键坑**：API 返回的 `id`（30 char hex）**不等于** `hcno`（32 char hex）。
> hcno 必须从详情页 JS 中提取，不能直接用 API id 替代！

```
[1] 扫描API (AJAX)
    POST/GET → https://std.samr.gov.cn/gb/search/gbQueryPage
               ?pageSize=50&pageNumber=1&searchText=&sortOrder=asc
    返回: {total, rows: [{id: "52A329FA...",  C_C_NAME, C_STD_CODE, STATE, ...}]}
    入口页: https://std.samr.gov.cn/gb/gbQuery

[2] 详情页 (提取 hcno)
    GET → https://std.samr.gov.cn/gb/search/gbDetailed?id={30-char-api-id}
    HTML 中 JS 正则: newGbInfo\?hcno=([A-Fa-f0-9]+)
    hcno 示例: 168FCE0B51AA6BDA00D9655728E0D22F

[3] 下载决策 (openstd.samr.gov.cn)
    GET → https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno={hcno}
    检测 HTML 中的按钮：
    - <button class="...xz_btn...">    → 有下载按钮 → 走验证码下载
    - <button class="...ck_btn...">    → 有预览按钮 → 走 Playwright 预览拼图
    - 检测到 "涉及版权保护" / "ISO、IEC" / "不提供在线阅读" → 版权限制

[4] 验证码下载流（xz_btn 路径）
    GET showGb?type=download&hcno=xxx     → 建立 session
    GET gc?_timestramp                     → 获取验证码图片
    POST verifyCode  verifyCode=ABCD      → 提交验证码（返回 'success' 继续）
    GET viewGb?hcno=xxx                    → 下载 PDF（验证 content-type + %PDF- 魔数）

[5] 预览下载流（ck_btn 路径，需 allow_preview=true）
    启动 Playwright → 打开页面 → 截图拼图块 → 合成单页 PDF
```

### dlStatus 状态枚举（跨 GB/HB/DB 统一）

| 状态 | 含义 | 是否可重试 |
|------|------|-----------|
| `downloaded` | 直接下载成功 | - |
| `previewed` | 预览拼接下载成功 | - |
| `skipped_existing` | 文件已存在，跳过 | - |
| `failed` | 下载失败（通用） | ✅ 重试 |
| `failed_hcno` | hcno 提取失败（详情页无 newGbInfo 跳转） | ✅ 重试会先重提取 |
| `no_hcno` | hcno 未分配（详情页有 newGbInfo 但 hcno 为空，标准太新未发布到 openstd） | ❌ 等待网站发布 |
| `failed_preview` | 预览拼接失败 | ✅ 重试 |
| `copyright` | 版权保护，不可下载 | ❌ |
| `preview_disabled` | 有预览按钮但用户关闭了预览功能 | ❌ |
| `no_fulltext` | 无全文/未收录 | ❌ |
```

### 搜索入口

```
用户搜索页面: https://std.samr.gov.cn/search/std?q=安全色
搜索结果 (iframe): https://std.samr.gov.cn/search/stdPage?q=安全色&tid=
结果链接格式: /gb/search/gbDetailed?id={30-char-api-id}
```

### 行标(HB) / 地标(DB) 扫描入口（2026-06实测）

> 两个网站结构相同，仅域名前缀不同（`hbba` / `dbba`）。

```
入口页: https://hbba.sacinfo.org.cn/stdList     (行业标准)
入口页: https://dbba.sacinfo.org.cn/stdList     (地方标准)

AJAX API (POST):
  HB: https://hbba.sacinfo.org.cn/stdQueryList
  DB: https://dbba.sacinfo.org.cn/stdQueryList

请求参数: current=1&size=15&key=&industry=&status=现行
返回: {total, current, pages, records: [{pk, code, chName, industry, status}]}
      pk 为 sha256 哈希，用作详情页/下载的唯一标识
```

---

## 项目结构

```
├── main.py                    # 桌面入口（pywebview 窗口 + 系统托盘）
├── ui.html                    # WebUI 界面（单文件 SPA）
├── version.py                 # 版本号集中管理（唯一来源）
├── verify_scripts.py          # 模块导入 + 语法验证（CI 用）
├── build.py                   # PyInstaller 打包脚本（CI 用）
├── AGENTS.md / README.md      # 文档
├── requirements.txt           # Python 依赖清单
├── ruff.toml                  # Ruff 代码检查配置
├── package.json               # 前端资源构建脚本（Tailwind CSS 编译）
│
├── .github/workflows/         # CI（ci.yml：语法检查+测试+PyInstaller 编译+Release）
├── src/tailwind.css           # Tailwind 源文件（npm run build:css 编译到 static/css/app.css）
├── static/                    # 静态资源（FastAPI 挂载到 /static，打包时随 exe 分发）
│   ├── css/app.css            # 编译后的应用样式（由 src/tailwind.css 生成，勿手改）
│   ├── css/font-awesome.min.css + fonts/   # 图标字体
│   ├── icon.ico / icon_64.png / logo.svg   # 应用图标/Logo
│   └── loading.html           # 启动加载页（pywebview 窗口首屏，API 就绪后自动跳转）
├── tests/                     # 单元测试（pytest，CI 运行）
│   ├── test_checkpoint.py / test_database.py / test_dedup.py
│   └── test_helpers.py / test_keywords.py / test_scanner_engine.py
├── tools/                     # 独立辅助工具（不参与应用运行，不被任何模块导入）
│   └── analyze_dup.py         # PDF 目录重复/近重复分析（CLI，可双击运行）
│
├── config/                    # 配置层 — 零业务依赖，被所有模块安全导入
│   ├── paths.py               # 路径集中管理（~/.std_scanner/）
│   ├── settings.py            # API端点 / 行业代码映射 / HTTP客户端常量
│   └── manager.py             # 配置管理（load/save/validate/mask）+ JSON持久化
│
└── app/                       # 业务层 — 所有应用逻辑
    ├── scanner/               # 核心扫描+下载（子包，从 scanner.py 拆分）
    │   ├── __init__.py        # 统一导出
    │   ├── gb_scan.py         # 国家标准扫描 + hcno提取 + download_phase
    │   ├── hb_scan.py         # 行业标准扫描 + 下载（含 _scan_list_standards 通用函数）
    │   ├── db_scan.py         # 地方标准扫描 + 下载
    │   ├── download.py        # 统一验证码下载（_unified_captcha_download + download_with_captcha）
    │   ├── preview.py         # 浏览器启动 + 预览转PDF（拼图块合成）
    │   ├── search.py          # 标准检索网站搜索（parsel 解析）
    │   ├── checkpoint.py      # 统一增量 checkpoint（gb/hb/db）
    │   ├── progress.py        # 进度保存（线程安全，时间间隔控制）
    │   ├── utils.py           # make_filename（文件名生成+安全清理）
    │   ├── change_tracker.py  # 标准变更快照对比
    │   └── quick.py           # CLI 入口（quick_download / quick_download_web / main）
    ├── scanner_engine.py      # 统一扫描引擎（run_scan_pipeline）
    ├── server.py              # FastAPI 应用入口（lifespan + 路由挂载）
    ├── routes/                # API 路由（子包，从 server.py 拆分）
    │   ├── __init__.py        # 路由注册 + health API + lifespan 事件
    │   ├── scan.py            # 扫描 API + batch_download
    │   ├── search.py          # 搜索下载 API
    │   ├── tasks.py           # 任务管理 API（含 retry）
    │   ├── scheduled.py       # 定时调度 API
    │   ├── config_routes.py   # 配置 + 关键词组 API
    │   ├── files.py           # 文件操作 API
    │   ├── checkpoint.py      # Checkpoint 管理 API
    │   ├── state.py           # 共享状态（task_manager / scheduler_mgr / ns）
    │   ├── sse.py             # SSE 实时推送
    │   └── _utils.py          # 路由共享工具（update_task_status / launch_task）
    ├── database.py            # SQLite 持久化（tasks/scheduled_jobs/notification_logs）
    ├── managers.py            # TaskManager（线程安全）+ SchedulerManager（APScheduler）
    ├── keywords.py            # 安全关键词匹配（113内置词 + 6排除词，多组管理）
    ├── captcha.py             # ddddocr 验证码封装（单例 + 图片预处理）
    ├── dedup.py               # 文件去重 / 缓存 / 文件监控
    ├── notifier.py            # 多渠道通知（Server酱 / PushPlus / 企业微信 / 钉钉）
    └── helpers.py             # 日志 / 格式化 / 路径安全
```

---

## 架构图

```
┌─────────────┐      ┌──────────────────┐      ┌──────────────────────┐
│   main.py   │─────▶│  app/server.py   │─────▶│  app/scanner/        │
│  (pywebview)│      │    (FastAPI)     │      │  (核心扫描+下载子包)   │
└─────────────┘      └──────┬───────────┘      └──────────┬───────────┘
                            │                             │
                     ┌──────┴───────────┐      ┌─────────┴───────────┐
                     │ app/routes/      │      │ app/scanner_engine  │
                     │  (API 路由子包)   │      │  (统一扫描编排)       │
                     └──────┬───────────┘      └─────────┬───────────┘
                            │                             │
                     ┌──────┴───────────┐      ┌─────────┴───────────┐
                     │ app/managers.py  │      │ app/notifier.py     │
                     │ app/database.py  │      │ app/helpers.py      │
                     └──────────────────┘      │ app/captcha.py      │
                                               │ app/dedup.py        │
                                               │ app/keywords.py     │
                                               └─────────────────────┘

         ┌──────────────────────────────────────────┐
         │  配置层（被所有模块依赖，零业务逻辑）         │
         │  config/paths.py → config/manager.py       │
         │  config/settings.py                        │
         └──────────────────────────────────────────┘
```
config/paths.py        ← 零依赖，定义路径常量
config/settings.py     ← 零依赖，定义运行时常量 + http_client（http2 降级保护）
config/manager.py      ← 依赖 paths.py
    ↓
app/helpers.py         ← 依赖 config/*
app/keywords.py        ← 依赖 config/manager.py
app/captcha.py         ← 零业务依赖
app/dedup.py           ← 依赖 config/manager.py
app/database.py        ← 依赖 config/paths.py
app/notifier.py        ← 依赖 config/manager.py + config/settings.py + database.py
app/managers.py        ← 依赖 database.py + notifier.py（启动时优先从 SQLite 加载）
    ↓
app/scanner/           ← 子包，各模块按需依赖
  checkpoint.py        ← 依赖 config/paths
  progress.py          ← 依赖 config/paths
  utils.py             ← 依赖 app/helpers（safe_filename）
  download.py          ← 依赖 config/settings, app/captcha
  search.py            ← 依赖 config/settings, parsel
  preview.py           ← 依赖 config/settings, app/captcha, app/dedup, PIL, playwright
  gb_scan.py           ← 依赖 config/settings, app/keywords, app/dedup, scanner 子模块
  hb_scan.py           ← 依赖 config/settings, app/keywords, app/dedup, scanner 子模块
  db_scan.py           ← 依赖 hb_scan（_scan_list_standards 复用）
  quick.py             ← 依赖所有 scanner 子模块（CLI 入口）
app/scanner_engine.py  ← 依赖 app/scanner（统一编排）
    ↓
app/routes/            ← 子包，依赖 scanner_engine + managers + state
  state.py             ← 共享全局单例（防御性检查）
  _utils.py            ← update_task_status + launch_task
  scan.py / search.py / tasks.py / scheduled.py / config_routes.py / files.py
app/server.py          ← 依赖 routes + managers + database + notifier + helpers
main.py                ← 依赖 server
```

---

## 所有模块详解

### config/paths.py — 路径集中管理

```python
CONFIG_DIR = Path.home() / ".std_scanner"        # 用户数据根目录
DB_PATH = CONFIG_DIR / "std_scanner.db"           # SQLite 数据库
CONFIG_FILE = CONFIG_DIR / "config.json"          # 配置文件
TASKS_FILE = CONFIG_DIR / "tasks.json"            # 任务（JSON 兼容层）
SCAN_CHECKPOINT_FILE = CONFIG_DIR / "scan_checkpoint.json"  # 统一增量 checkpoint

BASE_DIR = Path(__file__).parent.parent           # 项目根目录
DATA_FILE = BASE_DIR / "safety_full.json"         # 扫描结果 JSON
CKPT_FILE = BASE_DIR / "scan_ckpt.json"           # 旧版 checkpoint（保留兼容）
```

- **所有持久化路径**统一在此定义，禁止其他模块硬编码路径
- 用户数据存储在 `~/.std_scanner/`（与项目目录解耦）
- `migrate_old_data()` 自动从旧路径 (`项目根/.std_scanner/`) 迁移

### config/manager.py — 配置与任务持久化

关键函数：`load_config()` / `save_config()` / `deep_merge()` / `validate_config()` / `mask_sensitive_config()`

任务持久化（JSON 兼容层）：`save_tasks()` / `load_tasks()` / `clear_tasks()`
主存储为 SQLite（`app/database.py`），JSON 为兼容层。

`DEFAULT_CONFIG` 全局配置模板。

### config/settings.py — 运行时 API 常量

```python
http_client = httpx.Client(...)  # 全局连接池单例
HB_CODE_MAP                     # 行业代码 → 显示名映射
HB_SAFETY_CODES                 # 16个安全相关行业代码
DB_PROVINCE_MAP                 # 省份代码映射
PAGE_SIZE = 50                  # 国标每页条数
DELAY = 3.0                     # API 请求间隔（秒）
```

**重要规则**：所有 HTTP 请求必须使用全局 `http_client`，禁止每次新建。同步调用在 async 上下文中必须用 `run_in_executor` 包装。

### app/keywords.py — 安全关键词匹配

**数据模型**：多个"关键词组"（keyword groups）存储在 `config.json` 的 `keyword_groups` 字段。

每组结构：
```json
{
  "安全生产": {
    "keywords": ["安全", "消防", ...],    // 113个匹配词
    "excludes": ["信息安全", ...],         // 6个排除词
    "industries": ["AQ", "XF", ...],      // 行业筛选
    "provinces": ["江苏省"]               // 省份筛选
  }
}
```

- **预设 113 个安全关键词**（`_PRESET_KEYWORDS`），覆盖消防防爆、危化品、特种设备、职业卫生、应急管理、电气电力、建筑施工、工艺作业、氨制冷冷链、行业领域、爆炸物、场所风险
- **预设 6 个排除词**（`_PRESET_EXCLUDES`）：`信息安全、网络安全、数据安全、食品安全、农产品、饲料`
- **匹配顺序**：排除词先匹配（子串）→ 匹配词后匹配（子串）
- **所有关键词对 GB/HB/DB 所有类型标准都通用**，只有行业标准（HB）有行业筛选、地方标准（DB）有省份筛选
- 5 秒 TTL 缓存避免每次调用读磁盘

关键函数：
- `is_safety(text, std_type)` — 判断标准名称是否安全相关
- `is_aq_yj(code)` — 判断代码是否 AQ/YJ 开头
- `get_all_groups()` / `save_groups()` / `import_to_group()` / `delete_group()` / `reset_to_default()`
- `set_active_group(name)` / `get_active_group()` — 线程局部变量

### app/captcha.py — 验证码识别

```python
solve_captcha(image_bytes) → str  # 返回 4 位验证码
```

预处理流程：3x 放大 (LANCZOS) → 灰度 → 阈值 140 二值化 → ddddocr.classification() → 过滤非字母数字 → 转大写。OCR 识别率约 60-80%。

### app/dedup.py — 文件去重

- `get_existing_files()` — 获取已有文件名集合（600s 缓存窗口）
- `add_to_existing_files_cache()` — 下载成功后更新缓存
- `start_file_watcher()` / `stop_file_watcher()` — watchfiles 实时监控
- `invalidate_existing_dirs_cache()` / `get_dedup_stats()` — 公开接口

### app/notifier.py — 通知发送

单例 `NotificationService`，四种渠道：Server酱 / PushPlus / 企业微信 / 钉钉。
配置在 `~/.std_scanner/config.json` 的 `notifications` 字段，支持热更新。
所有 HTTP 请求复用全局 `http_client`（`config/settings.py`）。

### app/database.py — SQLite 持久化

三张表：`tasks` / `scheduled_jobs` / `notification_logs`
数据库：`~/.std_scanner/std_scanner.db`
初始化流程：`ensure_db()` → `init_db()` → `migrate_json_to_sqlite()`
**重要**：查询函数修改 `conn.row_factory = sqlite3.Row` 后必须用 try/finally 恢复原值，防止同一线程后续调用受影响。

### app/managers.py — 任务管理器

- **TaskManager**：线程安全（`threading.Lock`），内存 + JSON + SQLite 三层
  - 支持暂停/继续（`wait_if_paused` 协程）
  - 自动保存（10s 间隔）
- **SchedulerManager**：APScheduler 封装，支持 Cron 表达式

### app/helpers.py — 工具函数

- `setup_logger(name, log_level)` / `get_logger(name)` — 日志系统
- `safe_filename(filename)` — 清理非法字符 + 长度限制
- `format_bytes(n)` / `format_duration(seconds)` — 格式化
- `validate_path(path)` — 路径遍历攻击防护

### app/scanner/ — 核心扫描子包

从原 `scanner.py`（~1300行）拆分为 11 个子模块，职责清晰：

**gb_scan.py** — 国家标准扫描
```python
async def scan_pages(max_results=500, incr=False, keyword_group=None) -> list
async def extract_hcno(stds)          # 提取 hcno（run_in_executor 包装同步 HTTP）
async def download_phase(stds, existing=None)  # 下载阶段（run_in_executor 包装同步 HTTP）
def fetch_api_page(page, query=None) -> dict   # 国标分页查询（同步）
```

**hb_scan.py** — 行业标准扫描
```python
def scan_hb_standards(industries, max_results=500, incr=False, keyword_group=None) -> list
async def download_hb_standards(standards)      # 行业标准下载
def download_hb_with_captcha(hb_hash, site_type='hb')  # 验证码下载
def _scan_list_standards(items, ...)  # 行业/地方通用扫描函数（hb_scan + db_scan 复用）
```

**db_scan.py** — 地方标准扫描
```python
def scan_db_standards(provinces=None, max_results=500, incr=False, keyword_group=None) -> list
async def download_db_standards(standards)      # 地方标准下载
```

**download.py** — 统一验证码下载
```python
def _unified_captcha_download(dl_config, max_retries=8)  # 核心下载流程
def download_with_captcha(hcno)                           # 国标下载
# hb/db 的下载在 hb_scan.py 中（因为验证流程不同）
```

**preview.py** — 预览转 PDF
```python
async def launch_browser()                    # Chrome → Edge 降级
async def preview_to_pdf(hcno, filepath, ctx) # 拼图块合成 PDF
```

**search.py** — 标准检索网站搜索
```python
def fetch_stdpage_search(query, page, std_type) -> (list, int)
def check_downloadable(tid) -> (bool, str)
def check_hb_downloadable(detail_url) -> (bool, str, str)
```

**checkpoint.py** — 统一增量 checkpoint
```python
def load_scan_checkpoint() / _save_scan_checkpoint(data)
def get_incr_checkpoint(scan_type, item_key) / update_incr_checkpoint(...)
```

**progress.py** — 进度保存（线程安全，时间间隔控制）
```python
def save_progress(standards, force=False)
```

**utils.py** — 文件名生成（内部使用 `safe_filename` 清理）
```python
def make_filename(code, name) -> str
```

**quick.py** — CLI 入口
```python
async def quick_download(query)           # 搜索+一键下载
async def quick_download_web(query, ...)  # 网站搜索+下载
async def main()                          # CLI 入口（--pages/--scan-hb/--scan-db 等）
```

### app/scanner_engine.py — 统一扫描引擎

```python
async def run_scan_pipeline(std_type, config, task_id, task_manager) -> dict
```
- `run_scan_pipeline`: 单类型扫描编排（scan → extract → download）
- 所有同步 HTTP 调用均使用 `run_in_executor` 包装

### app/server.py — FastAPI 应用入口

精简为 lifespan + 路由挂载，具体 API 逻辑在 `app/routes/` 子包中。

### app/routes/ — API 路由子包

从原 `server.py`（~1600行）拆分为 10 个子模块：

- **__init__.py** — 路由注册 + health API + lifespan 事件
- **scan.py** — 扫描 API + `batch_download`（支持重试/并发/断点续传）
- **search.py** — 搜索下载 API
- **tasks.py** — 任务管理 API（含 retry，从任务数据正确提取参数）
- **scheduled.py** — 定时调度 API
- **config_routes.py** — 配置 + 关键词组 API
- **files.py** — 文件操作 API（含 `update_existing_dirs_api` 完整实现）
- **checkpoint.py** — Checkpoint 管理 API
- **state.py** — 共享全局单例（防御性检查，避免 None 错误）
- **sse.py** — SSE 实时推送（`GET /api/tasks/stream`）
- **_utils.py** — 路由共享工具（`update_task_status` + `launch_task` + `create_combined_scan_task`）

---

## 全部 API 接口

### 系统
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 返回 ui.html |
| GET | `/api/health` | 健康检查（系统/去重/任务统计） |
| GET | `/api/config` | 获取配置（敏感信息遮盖） |
| PUT | `/api/config` | 更新配置（热更新） |
| GET | `/api/industries` | 行业代码映射 |

### 关键词管理
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/keyword_groups` | 获取所有关键词组摘要 |
| PUT | `/api/keyword_groups` | 全量覆盖保存关键词组 |
| POST | `/api/keyword_groups/import` | 批量导入关键词到指定组 |
| DELETE | `/api/keyword_groups/{name}` | 删除关键词组（安全生产不可删） |
| POST | `/api/keyword_groups/reset` | 重置为预设默认值 |
| GET | `/api/keywords` | 获取当前活跃组关键词列表（兼容旧 API） |
| PUT | `/api/keywords` | 更新安全生产组 keywords 字段（兼容旧 API） |
| POST | `/api/keywords/reload` | 重新加载安全生产组（兼容旧 API） |
| POST | `/api/keywords/reset` | 重置安全生产组为预设（兼容旧 API） |

### 扫描（均支持 keyword_group 参数，默认 '安全生产'）
| 方法 | 路径 | 参数 |
|------|------|------|
| POST | `/api/scan/gb` | `max_results`(500), `scan_only`, `incr`, `keyword_group`, `resume_task_id` |
| POST | `/api/scan/hb` | `industries`, `max_results`(500), `scan_only`, `incr`, `keyword_group`, `resume_task_id` |
| POST | `/api/scan/db` | `provinces`, `max_results`(500), `scan_only`, `incr`, `keyword_group`, `resume_task_id` |
| POST | `/api/scan/all` | `types`(["gb","hb","db"]), `max_results`(500, 每个类型各扫描500条), `scan_only`, `incr`, `keyword_group` |

### 搜索
| 方法 | 路径 | 参数 |
|------|------|------|
| POST | `/api/search/query` | `query`, `std_type`, `max_results` |
| POST | `/api/search/download` | `items[]` (tid/pid/code/name) |

### 任务管理
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/tasks?status=` | 任务列表（可选状态筛选） |
| GET | `/api/tasks/stream` | SSE 实时任务推送 |
| GET | `/api/task/{id}` | 任务摘要 |
| GET | `/api/task/{id}/detail` | 任务详情（含运行时长） |
| DELETE | `/api/task/{id}` | 删除任务 |
| DELETE | `/api/tasks` | 清除所有 |
| POST | `/api/task/{id}/pause` | 暂停 |
| POST | `/api/task/{id}/resume` | 继续 |
| POST | `/api/task/{id}/retry` | 重试（创建新任务，保留 keyword_group） |
| POST | `/api/task/{id}/priority` | 设置任务优先级 |

### 定时调度
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/scheduled_jobs` | 列表 |
| POST | `/api/scheduled_jobs` | 创建（Cron） |
| PUT | `/api/scheduled_jobs/{id}` | 更新 |
| DELETE | `/api/scheduled_jobs/{id}` | 删除 |
| POST | `/api/scheduled_jobs/{id}/run` | 手动触发 |

### 通知
| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/config/notifications` | 更新通知配置 |
| POST | `/api/test_notification` | 测试通知 |

### 文件操作
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/output_dir` | 获取输出目录 |
| POST | `/api/open_output_dir` | 资源管理器打开 |
| POST | `/api/open_file` | 系统默认程序打开 |
| POST | `/api/open_url` | 浏览器打开 |
| POST | `/api/select_folder` | pywebview 文件夹对话框 |
| POST | `/api/save_file_dialog` | pywebview 保存对话框 |
| GET | `/api/browse_folder?path=` | 浏览文件夹内容 |

### 去重
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/existing_dirs` | 去重文件夹列表（含有效性验证） |
| POST | `/api/existing_dirs` | 更新去重文件夹列表 |

---

## 配置结构

路径：`~/.std_scanner/config.json`

```json
{
  "download": {
    "output_dir": "C:\\Users\\...\\Downloads\\标准速递",
    "allow_preview": true,
    "preview_quality": 2.0,
    "concurrent": 1,
    "max_retries": 8
  },
  "notifications": {
    "serverchan": { "enabled": false, "key": "" },
    "pushplus": { "enabled": false, "key": "" },
    "wecom": { "enabled": false, "key": "", "agentid": "" },
    "dingtalk": { "enabled": false, "token": "", "secret": "" }
  },
  "logging": { "level": "INFO" },
  "existing_dirs": [],
  "keyword_groups": {
    "安全生产": {
      "keywords": ["安全", "消防", ...],
      "excludes": ["信息安全", ...],
      "industries": ["AQ", "XF", ...],
      "provinces": ["江苏省"]
    }
  }
}
```

---

## 数据流

### 国家标准(GB)扫描
```
scan_pages(max_results, incr, keyword_group)
  → incr=True: 加载 checkpoint → 检测首页 first_id 是否变化
  → fetch_api_page(p) [每页50条, 同步→run_in_executor]
  → is_safety(name) / is_aq_yj(code) 筛选（使用活跃关键词组）
  → save_ckpt (每5页)
  → extract_hcno (HTTP gbDetailed)
  → download_phase
      → HTTP 检测按钮 (.xz_btn / .ck_btn / 版权)
      → 下载: download_with_captcha (8次OCR重试)
      → 预览: launch_browser → preview_to_pdf (拼图块合成)
```

### 行业标准(HB) / 地方标准(DB)扫描
```
scan_hb_standards(industries, max_results, incr, keyword_group) [sync]
  → 遍历 industries → fetch_hb_list(industry, page) [100条/页]
  → incr: 加载各 item 的 checkpoint，检测首页 first_pk 变化
  → is_safety 筛选（使用活跃关键词组）
  → download_hb_standards [async]
      → download_hb_with_captcha (8次OCR重试)
```

### 联合扫描(all)
```
POST /api/scan/all { types: [...], max_results: 500, incr, keyword_group }
  → max_results 每个类型各扫描这么多条（每种类型独立使用完整的 max_results）
  → 按顺序依次启动各类型子扫描，每种类型独立完成 scan → extract/download
```

### 定时扫描
```
_do_scheduled_scan(scan_type, job_cfg)
  → 所有类型 incr=True（强制增量）
  → gb: scan_pages → extract_hcno → download_phase（自动持久化 + 下载）
  → hb/db: scan → download_hb/db_standards（自动持久化 + 下载）
  → all: 按顺序执行三种类型，结果全部落地
```

### 搜索下载
```
fetch_stdpage_search(query, page, std_type)
  → parsel 解析 HTML → 提取 tid/pid/code/name
  → check_downloadable(tid)
  → 国标: 提取 hcno → 下载/预览
  → 行标/地标: check_hb_downloadable → download_hb_with_captcha
```

### 验证码流程
```
获取验证码图片 → solve_captcha
  → 3x 放大 (LANCZOS)
  → 灰度转换
  → 阈值 140 二值化
  → ddddocr.classification()
  → 过滤非字母数字
  → 转大写
  → 如果 < 4 字符 → 重试（最多 8 次）
```

---

## 技术栈

| 库 | 用途 | 关键注意点 |
|---|------|-----------|
| httpx | HTTP 客户端 | 全局单例 `http_client`，同步调用需 `run_in_executor` |
| ddddocr | 验证码识别 | 单例 `_get_ocr()`，3x 放大预处理 |
| Pillow | 图片处理/PDF合成 | 预览拼图块 → 合成 PDF |
| Playwright | 浏览器渲染 | 仅预览启动，Chrome→Edge降级，用完即关 |
| parsel | HTML 解析 | 搜索页面 DOM 提取 |
| FastAPI | API 框架 | uvicorn 127.0.0.1:8000 |
| pywebview | 桌面窗口 | Windows 原生窗口（可选） |
| pystray | 系统托盘 | 显示/隐藏/退出/浏览器打开 |
| watchfiles | 文件监控 | Rust 底层，毫秒级响应 |
| APScheduler | 定时任务 | 后台线程，Cron 触发 |
| psutil | 系统监控 | health API 返回 CPU/内存 |
| SQLite | 持久化 | `~/.std_scanner/std_scanner.db` |

---

## 编码规范

1. **日志**：全部使用 `logging.getLogger('std_scraper')`，禁止在管线函数中使用 `print()`。CLI入口代码（`if __name__ == '__main__'`）可保留 `print()`。
2. **HTTP 请求**：全局 `http_client`（`config/settings.py`），禁止每次新建 client。包括 `notifier.py` 的通知发送也必须复用。
3. **版本号**：只在 `version.py` 修改，禁止其他文件硬编码版本号
4. **路径**：只在 `config/paths.py` 定义，其他模块引用
5. **模块导入**：禁止在函数内 `import` 已在文件顶部导入的模块（如 `re`、`json`）
6. **线程安全**：全局可变状态必须加锁保护（如 `_progress_lock` for `save_progress`）
7. **async 函数**：同步 HTTP 调用在 async 上下文中必须用 `run_in_executor` 包装，避免阻塞事件循环
8. **数据库 row_factory**：查询函数修改后必须 save/restore
9. **浏览器**：仅预览启动，用完立即关闭
10. **并发**：默认 `concurrent=1`，增加可能触发反爬
11. **任务状态流转**：`running → paused → running | completed | failed`
12. **文件名**：Windows 路径限制 260 字符，`make_filename` 已做截断

---

## 常见坑和注意事项

- **同步函数在 async 中**：hb/db 扫描是同步函数，在 server.py 中必须用 `run_in_executor(None, lambda: ...)` 包装
- **http_client 生命周期**：不要每次请求新建，不要调用 `.close()`。notifier.py 也已统一使用全局 client
- **CORS 不是 `*`**：只允许 127.0.0.1:8000 和 localhost:8000
- **keyword_group 默认 '安全生产'**：所有扫描端点都有此参数，前端可能传 '默认' 或 '安全生产'
- **老 API `/api/keywords` PUT**：接受的是 dict `{"keywords": [...]}`，内部保持 dict 结构而非覆写为 list
- **checkpoint 格式**：gb 用 `first_id`，hb/db 用 `first_pk`，不要搞混
- **去重**：`dedup.py` 已从 notifier.py 独立出来，文件监控功能也在此
- **定时任务**：创建独立事件循环，不要复用主循环。所有类型强制 `incr=True`。扫描结果会自动持久化并下载
- **联合扫描**：`max_results` 每种类型各自拿这么多，不是按类型数均分
- **数据库 row_factory**：查询函数修改后必须 save/restore，否则同一线程后续调用会受影响
- **Windows PyInstaller**：如需打包，确保 ddocr/Playwright 的二进制文件正确包含

---

## 修改记录（最近版本）

### v1.0.0 — 当前版本

- 完整的国标/行标/地标扫描+下载链路
- 安全关键词多组管理（113 预设词 + 6 排除词）
- SSE 实时任务推送
- 任务优先级（插队机制）
- 统一增量 checkpoint（gb/hb/db）
- 预览转 PDF（Playwright 拼图块合成）
- 多渠道通知（Server酱/PushPlus/企业微信/钉钉）
- 文件去重 + watchfiles 实时监控
- 定时扫描（APScheduler Cron）
- 标准变更快照对比

### v3.6.1 — 退出修复 + 品牌更名 + UI体验优化

- 修复程序退出时的 ASGI CancelledError
- 品牌更名：标准抓取工具 → 标准速递
- 优雅退出、系统设置自动保存、UI 全面优化

### v3.6.0 — 全面修复优化 + 新功能

- 修复 SSE 事件格式不匹配、search.py 阻塞事件循环
- SchedulerManager 线程锁、TaskManager SSE 广播深拷贝
- save_config / save_tasks 原子写入
- 任务优先级、AGENTS.md 自动同步脚本、SSE 实时推送路由

### v3.4.0 — 架构重构 + 全面修复

- 模块拆分：scanner.py → app/scanner/ 子包，server.py → app/routes/ 子包
- 新增 scanner_engine.py 统一扫描编排引擎
- 定时扫描结果持久化、联合扫描重试
- 数据库 row_factory 保护、日志规范化、线程安全

### 更早版本

- v3.3.7 — 全面代码审查修复（关键词缓存、懒导入提升、CORS 常量提取）
- v3.3.5 — 关键词精简（185 → 113 词）
- v3.3.4 — 统一关键词组（排除词作为组内列表）
- v3.3.0 — 统一增量 checkpoint + 关键词组系统
