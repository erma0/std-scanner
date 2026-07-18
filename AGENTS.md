# Agent 上下文文档 — 标准速递 v1.1.0

> 本文档供 AI 助手理解项目全貌。用户文档见 `README.md`。
> 如果你是 CodeBuddy 或其他 AI 编程助手，以下是你需要知道的全部。

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

### 国标(GB)完整下载链路

> **⚠️ 关键坑**：API 返回的 `id`（30 char hex）**不等于** `hcno`（32 char hex）。
> hcno 必须从详情页 JS 中提取，不能直接用 API id 替代！

```
[1] 扫描API (AJAX, GET)
    → https://std.samr.gov.cn/gb/search/gbQueryPage
      ?pageSize=50&pageNumber=1&sortOrder=desc&sortName=id&state=G_STATE:"现行"
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

[5] 预览下载流（ck_btn 路径，需 allow_preview=true 且 Playwright 已安装）
    启动 Playwright → 打开页面 → 截图拼图块 → 合成单页 PDF
```

### 行标(HB) / 地标(DB) 扫描入口

> 两个网站结构相同，仅域名前缀不同（`hbba` / `dbba`）。

```
入口页: https://hbba.sacinfo.org.cn/stdList     (行业标准)
入口页: https://dbba.sacinfo.org.cn/stdList     (地方标准)

AJAX API (POST, application/x-www-form-urlencoded):
  HB: https://hbba.sacinfo.org.cn/stdQueryList
  DB: https://dbba.sacinfo.org.cn/stdQueryList

请求参数: current=1&size=100&key=&industry=&status[]=现行
返回: {total, current, pages, records: [{pk, code, chName, industry, status}]}
      pk 为 sha256 哈希，用作详情页/下载的唯一标识
```

### HB/DB 下载链路（无需验证码）

> **⚠️ 与 GB 不同**：HB/DB 直接 GET `/portal/download/{pk}` 即可返回 PDF，
> 不走验证码流程。`download_hb_with_captcha` 函数名保留 "captcha" 仅向后兼容。

```
[1] GET /stdDetail/{pk}        建立 session（下发 cookie）
[2] GET /portal/download/{pk}  直接下载 PDF（用 pk，不是 download_code）
[3] 若返回空/非 PDF → 访问 /portal/online/{pk} 检测是否"尚未公开"
    - 是 → 抛 CopyrightError（不可重试，dlStatus='copyright'）
    - 否 → 当作普通失败重试
```

实测各行业下载可用性：AQ/XF/JG/DL/SL 可下载；YS/HG/JT 版权限制。

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

### 搜索入口

```
用户搜索页面: https://std.samr.gov.cn/search/std?q=安全色
搜索结果 (iframe): https://std.samr.gov.cn/search/stdPage?q=安全色&tid=
结果链接格式: /gb/search/gbDetailed?id={30-char-api-id}
```

---

## 项目结构

```
├── main.py                    # 桌面入口（pywebview 窗口 + 系统托盘 + uvicorn）
├── ui.html                    # WebUI 界面（单文件 SPA）
├── version.py                 # 版本号集中管理（唯一来源，当前 v1.1.0）
├── verify_scripts.py          # 模块导入 + 语法验证（CI 用）
├── build.py                   # PyInstaller 打包脚本（CI 用，支持 --onefile/--clean）
├── AGENTS.md / README.md      # 文档
├── requirements.txt           # Python 依赖清单（Playwright 注释为可选）
├── ruff.toml                  # Ruff 代码检查配置（line-length=120）
├── package.json               # 前端资源构建脚本（Tailwind CSS v4 编译）
│
├── .github/workflows/         # CI（ci.yml：ruff+pytest+PyInstaller 编译+Release）
├── src/tailwind.css           # Tailwind 源文件（npm run build:css 编译到 static/css/app.css）
├── static/                    # 静态资源（FastAPI 挂载到 /static，打包时随 exe 分发）
│   ├── css/app.css            # 编译后的应用样式（由 src/tailwind.css 生成，勿手改）
│   ├── css/font-awesome.min.css + fonts/   # 图标字体
│   ├── icon.ico / icon_64.png / logo.svg   # 应用图标/Logo
│   └── loading.html           # 启动加载页（pywebview 窗口首屏，API 就绪后自动跳转）
├── tests/                     # 单元测试（pytest，CI 运行，共 46 个用例）
│   ├── test_checkpoint.py / test_database.py / test_dedup.py
│   └── test_helpers.py / test_keywords.py / test_scanner_engine.py
├── tools/                     # 独立辅助工具（不参与应用运行，不被任何模块导入）
│   └── analyze_dup.py         # PDF 目录重复/近重复分析（CLI，可双击运行）
│
├── config/                    # 配置层 — 零业务依赖，被所有模块安全导入
│   ├── paths.py               # 路径集中管理（~/.std_scanner/）
│   ├── settings.py            # API端点 / 行业代码映射 / HTTP客户端 / captcha client 工厂
│   └── manager.py             # 配置管理（load/save/validate/mask）+ JSON持久化
│
└── app/                       # 业务层 — 所有应用逻辑
    ├── scanner/               # 核心扫描+下载（子包，13 个模块）
    │   ├── __init__.py        # 统一导出
    │   ├── gb_scan.py         # 国家标准扫描 + hcno提取 + download_phase
    │   ├── hb_scan.py         # 行业标准扫描 + 下载（含 _scan_list_standards 通用函数）
    │   ├── db_scan.py         # 地方标准扫描 + 下载
    │   ├── download.py        # GB 验证码下载（download_with_captcha）
    │   ├── download_helpers.py # 共享：按钮检测 + hcno提取 + PDF落盘（跨 gb/hb/db/search/quick 复用）
    │   ├── preview.py         # 浏览器启动 + 预览转PDF（拼图块合成）
    │   ├── search.py          # 标准检索网站搜索（parsel 解析）
    │   ├── checkpoint.py      # 统一增量 checkpoint（gb/hb/db，含 reset）
    │   ├── progress.py        # 进度保存（线程安全，时间间隔控制）
    │   ├── utils.py           # make_filename（文件名生成+安全清理）
    │   ├── change_tracker.py  # 标准变更快照对比
    │   └── quick.py           # CLI 入口（quick_download / quick_download_web / main）
    ├── scanner_engine.py      # 统一扫描引擎（run_scan_pipeline，GB/HB/DB 共用编排）
    ├── server.py              # FastAPI 应用入口（lifespan + 路由挂载）
    ├── routes/                # API 路由（子包，11 个模块）
    │   ├── __init__.py        # 路由注册 + health API + lifespan 事件（含资源清理）
    │   ├── scan.py            # 扫描 API（gb/hb/db/all，支持 std_state/allow_preview）
    │   ├── search.py          # 搜索下载 API
    │   ├── tasks.py           # 任务管理 API（含 retry/retry-item/retry-failed/priority）
    │   ├── scheduled.py       # 定时调度 API
    │   ├── config_routes.py   # 配置 + 关键词组 API + playwright_status
    │   ├── files.py           # 文件操作 API
    │   ├── checkpoint.py      # Checkpoint 管理 API（含细粒度 reset）
    │   ├── state.py           # 共享状态（task_manager / scheduler_mgr / ns / _main_loop）
    │   ├── sse.py             # SSE 实时推送
    │   └── _utils.py          # 路由共享工具（update_task_status / launch_task / create_combined_scan_tasks）
    ├── database.py            # SQLite 持久化（tasks/scheduled_jobs/notification_logs）
    ├── managers.py            # TaskManager（线程安全）+ SchedulerManager（APScheduler）
    ├── keywords.py            # 安全关键词匹配（113内置词 + 6排除词，多组管理）
    ├── captcha.py             # ddddocr 验证码封装（多预处理策略 + Otsu 自适应阈值）
    ├── dedup.py               # 文件去重 / 缓存 / watchfiles 文件监控
    ├── notifier.py            # 多渠道通知（Server酱 / PushPlus / 企业微信 / 钉钉）
    └── helpers.py             # 日志 / 格式化 / 路径安全 / 原子写入 / normalize_code
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
         │  config/settings.py（http_client + captcha 工厂）│
         └──────────────────────────────────────────┘
```

依赖层次：
```
config/paths.py        ← 零依赖，定义路径常量
config/settings.py     ← 零依赖，定义运行时常量 + http_client（http2 降级保护）+ captcha client 工厂
config/manager.py      ← 依赖 paths.py
    ↓
app/helpers.py         ← 依赖 config/*
app/keywords.py        ← 依赖 config/manager.py
app/captcha.py         ← 零业务依赖（ddddocr + PIL）
app/dedup.py           ← 依赖 config/manager.py
app/database.py        ← 依赖 config/paths.py
app/notifier.py        ← 依赖 config/manager.py + config/settings.py + database.py
app/managers.py        ← 依赖 database.py + notifier.py（启动时优先从 SQLite 加载）
    ↓
app/scanner/           ← 子包，各模块按需依赖
  checkpoint.py        ← 依赖 config/paths
  progress.py          ← 依赖 config/paths
  utils.py             ← 依赖 app/helpers（safe_filename）
  download_helpers.py  ← 依赖 config/settings（正则 + 按钮检测 + 落盘）
  download.py          ← 依赖 config/settings, app/captcha, download_helpers
  search.py            ← 依赖 config/settings, parsel
  preview.py           ← 依赖 config/settings, app/captcha, app/dedup, PIL, playwright
  gb_scan.py           ← 依赖 config/settings, app/keywords, app/dedup, scanner 子模块
  hb_scan.py           ← 依赖 config/settings, app/keywords, app/dedup, scanner 子模块
  db_scan.py           ← 依赖 hb_scan（_scan_list_standards / _download_standards 复用）
  quick.py             ← 依赖所有 scanner 子模块（CLI 入口）
app/scanner_engine.py  ← 依赖 app/scanner（统一编排，不调用 extract_hcno）
    ↓
app/routes/            ← 子包，依赖 scanner_engine + managers + state
  state.py             ← 共享全局单例（防御性检查）
  _utils.py            ← update_task_status + launch_task + create_combined_scan_tasks
  scan.py / search.py / tasks.py / scheduled.py / config_routes.py / files.py / checkpoint.py
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
TASKS_FILE = CONFIG_DIR / "tasks.json"            # 任务 JSON 兼容层（旧迁移源）
SCAN_CHECKPOINT_FILE = CONFIG_DIR / "scan_checkpoint.json"  # 统一增量 checkpoint
LOG_DIR = CONFIG_DIR / "logs"                     # 日志目录

BASE_DIR = Path(__file__).parent.parent           # 项目根目录
DATA_FILE = CONFIG_DIR / "safety_full.json"       # 扫描结果 JSON（已迁移到 CONFIG_DIR）
CKPT_FILE = CONFIG_DIR / "scan_ckpt.json"         # 旧版 checkpoint（保留兼容）
STATIC_DIR = BASE_DIR / "static"
UI_FILE = BASE_DIR / "ui.html"
```

- **所有持久化路径**统一在此定义，禁止其他模块硬编码路径
- 用户数据存储在 `~/.std_scanner/`（与项目目录解耦）
- `migrate_old_data()` 自动从旧路径 (`项目根/.std_scanner/`) 迁移

### config/manager.py — 配置持久化

关键函数：`load_config()` / `save_config()`（原子写入）/ `deep_merge()` / `validate_config()` / `mask_sensitive_config()`

`DEFAULT_CONFIG` 模板（关键字段）：

```python
{
    "download": {
        "output_dir": "~/Downloads/安全标准",
        "existing_dirs": [],
        "duplicate_check_strategy": "early",
        "delay": 3.0,
        "max_retries": 3,
        "retry_delay": 2.0,
        "concurrent": 1,           # 下载并发度，上限 10
        "max_network_retries": 2,
        "strategy": "full",        # 'full' | 'scan_only'
        "allow_preview": True,
        "preview_quality": 0.6,    # 0.3-1.0，默认 0.6（非 2.0）
    },
    "notifications": {
        "serverchan": {"enabled": False, "sckey": ""},   # 字段是 sckey
        "pushplus":   {"enabled": False, "token": ""},   # 字段是 token
        "wecom":      {"enabled": False, "webhook": ""}, # 无 agentid
        "dingtalk":   {"enabled": False, "webhook": "", "secret": ""},
    },
    "logging": {"level": "INFO", "save_to_file": True},
    "tasks": {                     # 任务清理策略
        "auto_save": True,
        "save_interval": 10,
        "retention_hours": 168,    # 7 天
        "max_tasks": 200,
    },
    "keyword_groups": { "安全生产": {...} },
}
```

### config/settings.py — 运行时常量 + HTTP 客户端 + captcha 工厂

```python
# 端点
API_BASE = "https://std.samr.gov.cn/gb/search/gbQueryPage"
DETAIL_URL = "https://std.samr.gov.cn/gb/search/gbDetailed"
OPENSTD = "https://openstd.samr.gov.cn/bzgk"
GB_DOWNLOAD_BASE = "https://openstd.samr.gov.cn/bzgk/std"
SEARCH_PAGE = "https://std.samr.gov.cn/search/stdPage"
HB_API_URL = "https://hbba.sacinfo.org.cn/stdQueryList"
DB_API_URL = "https://dbba.sacinfo.org.cn/stdQueryList"

# 运行时
PAGE_SIZE = 50                  # 国标每页条数
DELAY = 3.0                     # 启动时快照（兼容旧导入）
get_delay()                     # 5s TTL 缓存，从 download.delay 实时读取
get_output_dir()                # 5s TTL 缓存，从 download.output_dir 读取
BROWSER_CHANNELS = ['chrome', 'msedge']  # 预览浏览器降级链

# 全局 HTTP 客户端（单例，http2 失败自动降级 http1.1）
http_client = httpx.Client(http2=True, follow_redirects=True, ...)

# captcha client 工厂（下载并发场景使用）
get_captcha_client(site_type)      # 共享 client（单线程场景）
create_captcha_client(site_type)   # 独立 client（并发场景，调用方负责关闭）
close_captcha_clients()            # 关闭所有共享 client（atexit + lifespan 调用）

# 行业/省份映射
HB_CODE_MAP            # 79 个行业代码 → 显示名映射
HB_SAFETY_CODES        # 16 个安全相关行业代码
DB_PROVINCE_MAP        # 31 个省/直辖市拼音 → 6 位行政区划代码
_resolve_hb_industry(code_or_name)  # 输入代码或中文名 → 中文的行业名
```

**重要规则**：所有 HTTP 请求必须使用全局 `http_client`，禁止每次新建。同步调用在 async 上下文中必须用 `run_in_executor` 包装。下载并发任务用 `create_captcha_client` 创建独立 client 避免验证码 session 串扰。

### app/keywords.py — 安全关键词匹配

**数据模型**：多个"关键词组"（keyword groups）存储在 `config.json` 的 `keyword_groups` 字段。

每组结构：
```json
{
  "安全生产": {
    "keywords": ["安全", "消防", ...],    // 113个匹配词
    "excludes": ["信息安全", ...],         // 6个排除词
    "industries": ["AQ", "XF", ...],      // 行业筛选（仅 HB 用）
    "provinces": ["江苏省"]               // 省份筛选（仅 DB 用）
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
solve_captcha(img_data: bytes) -> str  # 返回 4 位验证码
```

多预处理策略（v3.8.3 重构，原图识别优先）：
1. **原图直接识别** — 不做预处理，≥4 字符立即返回
2. **basic** — 灰度 + Otsu 自适应阈值（替代固定阈值 140）
3. **enhanced** — 2x 放大 + 灰度 + Otsu
4. **denoise** — 2x 放大 + 灰度 + 中值滤波 + Otsu

任一策略 ≥4 字符即返回，取最长结果。OCR 识别率约 60-80%。

> **注意**：`close_captcha_clients` / `create_captcha_client` / `get_captcha_client` 不在此模块，
> 在 `config/settings.py`（因为依赖 httpx 配置）。

### app/dedup.py — 文件去重

- `get_existing_files(force_refresh=False)` — 获取已有文件名集合（600s 缓存窗口）
- `get_existing_dirs()` — 去重目录列表
- `add_to_existing_files_cache(filename)` — 下载成功后更新缓存
- `start_file_watcher()` / `stop_file_watcher()` — watchfiles 实时监控（线程优雅停止）
- `invalidate_existing_dirs_cache()` / `get_dedup_stats()` — 公开接口

### app/notifier.py — 通知发送

单例 `NotificationService`，四种渠道：
- `ServerChanNotifier(sckey)` — Server酱
- `PushPlusNotifier(token)` — PushPlus
- `WeComNotifier(webhook)` — 企业微信（无 agentid）
- `DingTalkNotifier(webhook, secret="")` — 钉钉

配置在 `~/.std_scanner/config.json` 的 `notifications` 字段，支持热更新。
所有 HTTP 请求复用全局 `http_client`。发送结果写入 `notification_logs` 表。

关键函数：`get_notification_service(force_reload=False)` / `reset_notification_service()` / `send_report()` / `send_message()` / `format_report()`

### app/database.py — SQLite 持久化

三张表，数据库路径 `~/.std_scanner/std_scanner.db`：

- **tasks** — 主存储（id/task_type/status/progress/message/stats/sub_stats/start_time/end_time/paused_duration/paused_at/created_at/updated_at + ALTER 追加列：std_items/keyword_group/max_results/incr/scan_only/industries/provinces/changes/priority）
- **scheduled_jobs** — 镜像存储（主存储是 config.json 的 scheduled_jobs 字段）
- **notification_logs** — 通知日志（30 天自动清理）

初始化流程：`ensure_db()` → `init_db()` → `migrate_json_to_sqlite()`
**重要**：查询函数修改 `conn.row_factory = sqlite3.Row` 后必须用 try/finally 恢复原值。
线程本地连接（WAL 模式，timeout=10s）。

关键函数：`save_task` / `save_task_light`（轻量持久化，跳过 std_items）/ `get_task` / `get_all_tasks`（按 created_at DESC）/ `delete_task` / `cleanup_notification_logs`

### app/managers.py — 任务管理器

**TaskManager**：线程安全（`threading.Lock`），内存 + SQLite 双层
- `get(task_id)` / `get_all(status_filter=None)` — get_all 按 `created_at` 倒序（缺失时回退 `start_time`），新任务排在最前
- `create(task_id, task_data)` / `create_with_priority(task_id, task_data, priority)`
- `update(task_id, status, progress, message, stats, persist_std_items, **kwargs)` — 自动广播 SSE
- `pause(task_id)` / `resume(task_id)` / `is_paused(task_id)`
- `delete(task_id)` / `delete_all()` / `exists(task_id)`
- `increment_stats(task_id, **increments)`
- `bump_priority(task_id, delta=1)` / `get_pending_by_priority()`
- `count_by_status()` / `cleanup_completed(max_age_hours=168, max_tasks=200)`
- `mark_interrupted()` — 启动时把 running/paused 任务标记为 interrupted，补 end_time
- `save_all()`
- `_broadcast_sse(task_snapshot)` — 私有，动态注入 duration 字段

> **注意**：`wait_if_paused` 已删除，被扫描/下载函数的 `check_pause` 回调参数替代。

**SchedulerManager**：APScheduler 封装，支持 Cron 表达式
- 主存储是 `config.json` 的 `scheduled_jobs` 字段
- `start()` / `shutdown()` / `available` property
- `load_jobs()` / `save_jobs()` / `add_job(job_id, job_config, run_fn)` / `remove_job()` / `update_job()` / `toggle_job()`
- `get_job()` / `get_all_jobs()` / `get_next_run_times()`

模块级单例：`task_manager = TaskManager()` / `scheduler_manager = SchedulerManager()`

### app/helpers.py — 工具函数

- `setup_logger(name, log_level, log_dir)` / `get_logger()` — 日志系统（同时输出 stdout + 文件）
- `normalize_code(code)` — 清理 `<sacinfo>` HTML 标签 + 统一 dash 字符变体（GB/HB/DB/Search/ChangeTracker 共用）
- `safe_filename(filename)` — 清理非法字符 + dash 标准化 + 150 字符长度限制
- `format_bytes(size_bytes)` / `format_duration(seconds)` — 格式化
- `validate_path(filepath, base_dir=None)` — 路径遍历攻击防护，返回规范化绝对路径或 None
- `atomic_write(filepath, data, mode, encoding, dir_)` — 原子写入（tempfile + os.replace）

### app/scanner/ — 核心扫描子包

13 个子模块，职责清晰：

**gb_scan.py** — 国家标准扫描
```python
async def scan_pages(
    max_results=500, incr=False, keyword_group=None,
    on_progress=None,            # async callable(pct, msg)
    check_pause=None,            # async callable() → bool
    on_intermediate=None,        # async callable(standards_list)
    state='现行',                # '' | '现行' | '即将实施' | '废止'
) -> list
async def extract_hcno(standards, on_progress=None)  # 独立调用入口（scanner_engine 不再调用）
async def download_phase(
    standards, existing=None, allow_preview_override=None,
    on_progress=None, on_item_done=None, check_pause=None,
)  # 已合并 extract_hcno 串行阶段；支持并发（config.download.concurrent）
def fetch_api_page(page, state='', query=None) -> dict  # 同步
```

**hb_scan.py** — 行业标准扫描
```python
def fetch_hb_list(industry='', key='', status=None, page=1, size=100) -> dict  # 同步
def scan_hb_standards(
    industries=None, max_results=500, incr=False, keyword_group=None,
    on_progress=None, on_intermediate=None, check_pause=None,  # 同步 callable
    status='现行',
) -> list
async def download_hb_standards(standards, on_progress, on_item_done, check_pause)
def download_hb_with_captcha(hb_hash, site_type='hb', client=None)  # 实际不走验证码
async def _download_standards(standards, log_prefix, on_progress, on_item_done, check_pause)  # HB/DB 共用
def _scan_list_standards(items, item_label, fetch_fn, site_type, log_prefix,
                         max_results=500, incr=False,
                         on_progress=None, on_intermediate=None, check_pause=None)  # HB/DB 通用扫描
class CopyrightError(Exception)  # 版权限制异常（不可重试）
```

**db_scan.py** — 地方标准扫描（复用 hb_scan 的 `_scan_list_standards` / `_download_standards`）
```python
def fetch_db_list(province='', key='', status=None, page=1, size=100) -> dict
def scan_db_standards(provinces=None, max_results=500, incr=False, keyword_group=None,
                      on_progress=None, on_intermediate=None, check_pause=None,
                      status='现行') -> list  # '即将实施' 自动回退到 '现行'
async def download_db_standards(standards, on_progress, on_item_done, check_pause)
```

**download.py** — GB 验证码下载
```python
def download_with_captcha(hcno, client=None) -> Optional[bytes]  # client 可选用于并发
# 内部：_unified_captcha_download（DEFAULT_MAX_OCR_RETRIES=12, DEFAULT_MAX_NETWORK_RETRIES=3）
```

**download_helpers.py** — 共享下载工具（新增，跨 gb/hb/db/search/quick 复用）
```python
RE_XZ_BTN / RE_CK_BTN / RE_HCNO  # 正则常量
class DownloadButtons(NamedTuple):  # has_download / has_preview / copyright / can_download / can_preview
def detect_download_buttons(html) -> DownloadButtons
def extract_hcno_from_html(html) -> Optional[str]
def fetch_and_save_pdf(download_fn, filepath, filename, output_dir) -> Optional[bytes]
```

**preview.py** — 预览转 PDF
```python
PLAYWRIGHT_AVAILABLE: bool  # 模块常量
async def launch_browser() -> (playwright_mgr, browser_ctx)  # Chrome → Edge 降级
@asynccontextmanager
async def browser_session()  # 自动关闭的上下文管理器
async def preview_to_pdf(hcno, filepath, browser_context)
# 默认参数：_DEFAULT_PAGE_W=1190, _DEFAULT_PAGE_H=1680, _PUZZLE_GRID=10, _PDF_DPI=168
```

**search.py** — 标准检索网站搜索
```python
def fetch_stdpage_search(query, page=1, std_type='国家标准') -> (list, int)
def get_detail_url_by_tid(tid, pid) -> (detail_url, tid)  # 分发 HB/DB/GB Plan/GB
def check_downloadable(tid) -> (can_dl, msg)
def check_hb_downloadable(detail_url) -> (can_download, hb_hash, pattern)
```

**checkpoint.py** — 统一增量 checkpoint
```python
def load_scan_checkpoint() / _save_scan_checkpoint(data)
def get_incr_checkpoint(scan_type, item_key=None)
def update_incr_checkpoint(scan_type, item_key, data)
def reset_incr_checkpoint(scan_type=None, item_key=None)  # 三种粒度重置
def load_ckpt() / save_ckpt(data)  # 旧版兼容
```

**progress.py** — 进度保存（线程安全，时间间隔控制）
```python
def save_progress(standards, force=False)
```

**utils.py** — 文件名生成
```python
def make_filename(code, name) -> str  # 内部使用 safe_filename 清理
```

**change_tracker.py** — 标准变更快照对比
```python
def compare_snapshot(scan_type, standards) -> dict  # 检测 added/changed/removed
def compute_download_stats(standards) -> dict  # 统计 downloaded/failed/skipped 等
```

**quick.py** — CLI 入口
```python
async def quick_download(query)           # 搜索+一键下载
async def quick_download_web(query, ...)  # 网站搜索+下载
async def main()                          # CLI 入口
```

### app/scanner_engine.py — 统一扫描引擎

```python
async def run_scan_pipeline(
    scan_type: str,                # 'gb' | 'hb' | 'db'
    config: dict,                  # max_results/incr/keyword_group/scan_only/std_state/industries/provinces/allow_preview
    task_id: str = None,
    task_manager=None,
    progress_base: int = 0,
    progress_per_scan: int = 40,
    progress_per_download: int = 60,
) -> list
```

- 单一入口编排 GB/HB/DB 三种类型的 scan → download 流程，消除路由层三处扫描逻辑重复
- **阶段 1：扫描** — GB 直接 await，HB/DB 用 `run_in_executor` 包装同步函数
- **变更追踪** — 调用 `compare_snapshot` 与上次快照对比
- **阶段 2：下载** — GB 调 `download_phase`（已合并 hcno 提取，不再独立调 `extract_hcno`）；HB/DB 调 `download_hb_standards` / `download_db_standards`
- 所有同步 HTTP 调用均使用 `run_in_executor` 包装
- 通过 `task_manager` 统一更新进度，支持 `check_pause` 中止

### app/server.py — FastAPI 应用入口

精简为 lifespan + 路由挂载，具体 API 逻辑在 `app/routes/` 子包中。

### app/routes/ — API 路由子包

11 个子模块：

- **__init__.py** — 路由注册 + lifespan 事件（启动：`init_config_logger` / `sse_reset` / `mark_interrupted` / `cleanup_completed` / `cleanup_notification_logs` / `start_file_watcher` / `start scheduler`；关闭：`sse_close_all` / `scheduler.shutdown` / `stop_file_watcher` / `save_all` / `close_captcha_clients` / `http_client.close`）+ `GET /` + `GET /favicon.ico` + `GET /api/health`
- **scan.py** — 扫描 API（`/api/scan/gb` `/hb` `/db` `/all`），均支持 `std_state` / `keyword_group` / `resume_task_id` / `allow_preview`（仅 GB）参数；`/api/scan/all` 接收 `ScanAllRequest` body，通过 `create_combined_scan_tasks` 并发三任务
- **search.py** — `/api/search/query` + `/api/search/download`
- **tasks.py** — 任务管理 API（含 `retry` / `retry-item/{item_index}` / `retry-failed` / `priority`）
- **scheduled.py** — 定时调度 API（CRUD + `/run` 手动触发）
- **config_routes.py** — 配置 + 关键词组 API + `/api/playwright_status`（检查 Playwright 是否可用）
- **files.py** — 文件操作 API（含 `update_existing_dirs_api` 完整实现）
- **checkpoint.py** — Checkpoint 管理 API（`GET /api/checkpoint` 查看、`DELETE /api/checkpoint?scan_type=&item=` 细粒度重置，运行中扫描任务拒绝重置）
- **state.py** — 共享全局单例（`task_manager` / `scheduler_mgr` / `ns` / `_main_loop` / `pywebview_window`，含 `init_state()` 初始化函数和 getter 防御性检查）
- **sse.py** — SSE 实时推送（`GET /api/tasks/stream`，事件：`init` / `task_update` / `keepalive`）
- **_utils.py** — 路由共享工具（`update_task_status` + `launch_task` + `create_combined_scan_tasks`）

---

## 全部 API 接口

### 系统
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 返回 ui.html |
| GET | `/favicon.ico` | 应用图标 |
| GET | `/api/health` | 健康检查（version/database/dedup/scheduler） |
| GET | `/api/config` | 获取配置（敏感信息遮盖） |
| PUT | `/api/config` | 更新配置（热更新） |
| GET | `/api/industries` | 行业代码映射 |
| GET | `/api/playwright_status` | 检查 Playwright 是否可用 |

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

### 扫描（均支持 `keyword_group` 参数，默认 '安全生产'；均支持 `std_state`，默认 '现行'）
| 方法 | 路径 | 参数 |
|------|------|------|
| POST | `/api/scan/gb` | `max_results`(500), `scan_only`, `incr`, `keyword_group`, `resume_task_id`, `allow_preview`, `std_state` |
| POST | `/api/scan/hb` | `industries`(Query list), `max_results`, `scan_only`, `incr`, `keyword_group`, `resume_task_id`, `std_state` |
| POST | `/api/scan/db` | `provinces`(Query list), `max_results`, `scan_only`, `incr`, `keyword_group`, `resume_task_id`, `std_state` |
| POST | `/api/scan/all` | Body: `ScanAllRequest`（types/scan_only/incr/max_results/keyword_group/std_state/allow_preview/gb_config/hb_config/db_config） |

### 搜索
| 方法 | 路径 | 参数 |
|------|------|------|
| POST | `/api/search/query` | `query`, `std_type`, `max_results` |
| POST | `/api/search/download` | `items[]` (tid/pid/code/name) |

### 任务管理
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/tasks?status=` | 任务列表（按 created_at 倒序，动态计算 duration） |
| GET | `/api/tasks/stream` | SSE 实时任务推送 |
| GET | `/api/task/{id}` | 任务摘要 |
| GET | `/api/task/{id}/detail` | 任务详情（含运行时长） |
| DELETE | `/api/task/{id}` | 删除任务 |
| DELETE | `/api/tasks` | 清除所有 |
| POST | `/api/task/{id}/pause` | 暂停 |
| POST | `/api/task/{id}/resume` | 继续 |
| POST | `/api/task/{id}/retry` | 重试（search 任务拒绝；gb/hb/db 重建；all 拆三任务） |
| POST | `/api/task/{id}/retry-item/{item_index}` | 单条重试（不重新扫描，仅重下该条） |
| POST | `/api/task/{id}/retry-failed` | 批量重试所有失败项 |
| POST | `/api/task/{id}/priority` | 调整任务优先级（body: priority 非负整数） |

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
| POST | `/api/open_file` | 系统默认程序打开（body: path） |
| POST | `/api/open_url` | 浏览器打开（仅 http/https） |
| POST | `/api/select_folder` | pywebview 文件夹对话框 |
| POST | `/api/save_file_dialog` | pywebview 保存对话框（body: default_name, file_types） |
| GET | `/api/browse_folder?path=` | 浏览文件夹内容 |

### 去重
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/existing_dirs` | 去重文件夹列表（含有效性验证：exists/file_count） |
| POST | `/api/existing_dirs` | 更新去重文件夹列表（body: List[str]） |

### Checkpoint 管理
| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/checkpoint` | 查看全部 checkpoint（注入 HB 显示名） |
| DELETE | `/api/checkpoint?scan_type=&item=` | 重置（不传 = 全部；`?scan_type=gb` = 国标；`?scan_type=hb&item=AQ` = 单行业） |

---

## 配置结构

路径：`~/.std_scanner/config.json`

```json
{
  "download": {
    "output_dir": "C:\\Users\\...\\Downloads\\安全标准",
    "existing_dirs": [],
    "duplicate_check_strategy": "early",
    "delay": 3.0,
    "max_retries": 3,
    "retry_delay": 2.0,
    "concurrent": 1,
    "max_network_retries": 2,
    "strategy": "full",
    "allow_preview": true,
    "preview_quality": 0.6
  },
  "notifications": {
    "serverchan": {"enabled": false, "sckey": ""},
    "pushplus": {"enabled": false, "token": ""},
    "wecom": {"enabled": false, "webhook": ""},
    "dingtalk": {"enabled": false, "webhook": "", "secret": ""}
  },
  "logging": {"level": "INFO", "save_to_file": true},
  "tasks": {
    "auto_save": true,
    "save_interval": 10,
    "retention_hours": 168,
    "max_tasks": 200
  },
  "existing_dirs": [],
  "scheduled_jobs": {},
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
scan_pages(max_results, incr, keyword_group, state, on_progress, check_pause, on_intermediate)
  → incr=True: 加载 checkpoint，读 first_id
  → fetch_api_page(p, state) [每页50条, 同步→run_in_executor]
  → 增量短路：首页首条 id == last_first_id → 无新增，break
  → 增量逐条比对：遇到 row.id == last_first_id → 命中上次位置，break
  → is_safety(name) / is_aq_yj(code) 筛选（使用活跃关键词组）
  → update_incr_checkpoint (每5页 + 最终保存 first_id/first_code/first_name)
  → download_phase
      → 串行阶段：提取 hcno + 检测按钮 (.xz_btn / .ck_btn / 版权)
      → 并发阶段：download_with_captcha（独立 client，DEFAULT_MAX_OCR_RETRIES=12）
      → 预览阶段：launch_browser → preview_to_pdf（串行，Playwright 限制）
```

### 行业标准(HB) / 地方标准(DB)扫描
```
scan_hb_standards(industries, max_results, incr, keyword_group, status, ...) [sync, 线程内运行]
  → 遍历 industries → fetch_hb_list(industry, page, status) [100条/页]
  → incr: 加载各 item 的 checkpoint，读 first_pk
  → 增量短路：首页首条 pk == last_first_pk → 无新增，break
  → 增量逐条比对：遇到 r.pk == last_first_pk → 命中上次位置，break
  → is_safety 筛选（使用活跃关键词组）
  → update_incr_checkpoint（保存 first_pk/first_code/first_name/page/count）
  → _download_standards [async]
      → 并发（config.download.concurrent，独立 client）
      → download_hb_with_captcha(pk, site_type, client)（实际不走验证码）
      → 失败时检测 _is_copyright_restricted → 抛 CopyrightError
```

### 联合扫描(all)
```
POST /api/scan/all { types, max_results, scan_only, incr, keyword_group, std_state, allow_preview, ... }
  → create_combined_scan_tasks 返回 (task_ids, async_fn)
  → asyncio.gather 并发执行三个子任务（gb/hb/db 各自独立 max_results）
  → 每个子任务独立完成 scan → download
```

### 定时扫描
```
run_scheduled_scan(job_id, job_config)  [APScheduler 回调]
  → 若主循环未就绪 → 标记任务失败 + 通知用户（不再回退独立事件循环）
  → _do_scheduled_scan_impl(scan_type, job_cfg)
  → 所有类型 incr=True（强制增量）
  → all 类型：create_combined_scan_tasks 并发三任务，发汇总通知
```

### 搜索下载
```
fetch_stdpage_search(query, page, std_type)
  → parsel 解析 HTML → 提取 tid/pid/code/name
  → get_detail_url_by_tid(tid, pid) 分发到 GB/HB/DB/GB Plan 详情页
  → check_downloadable(tid) / check_hb_downloadable(detail_url)
  → 国标: 提取 hcno → download_with_captcha / preview_to_pdf
  → 行标/地标: download_hb_with_captcha(pk, site_type)
```

### 验证码流程（仅 GB）
```
获取验证码图片 → solve_captcha
  → 策略 1: 原图直接 ddddocr.classification()，≥4 字符返回
  → 策略 2-4: 依次尝试 basic / enhanced / denoise 预处理
    - basic:    灰度 + Otsu 自适应阈值
    - enhanced: 2x 放大 + 灰度 + Otsu
    - denoise:  2x 放大 + 灰度 + 中值滤波 + Otsu
  → 过滤非字母数字，转大写
  → 取最长结果，≥4 字符返回；否则重试（最多 12 次）
```

### 增量扫描机制（v1.1.0 优化）

**核心改动**：从"首页首条整体比对"改为"逐条精确比对，命中即停"。

| 类型 | 比对字段 | ckpt 存储 |
|------|---------|----------|
| GB | `row.id`（API 内部唯一 ID） | `first_id` + `first_code` + `first_name` |
| HB/DB | `r.pk`（sha256 主键） | `first_pk` + `first_code` + `first_name` |

**两层短路**：
1. **首页短路**：首页第一条 id/pk 与 ckpt 完全相同 → 无任何新数据，立即 break
2. **逐条命中**：遍历每条记录，遇到上次最新一条立即 break（该条及之后都已扫过）

**效果**：假设上次扫了 500 条，今天网站新发 5 条，只需扫前 5 条新数据 + 第 6 条命中即停（约 1 页），不再扫满 max_results。

---

## 技术栈

| 库 | 用途 | 关键注意点 |
|---|------|-----------|
| httpx | HTTP 客户端 | 全局单例 `http_client`（http2 自动降级），同步调用需 `run_in_executor`；并发下载用 `create_captcha_client` 独立实例 |
| ddddocr | 验证码识别 | 单例 `_get_ocr()`，多预处理策略 + Otsu 自适应阈值 |
| Pillow | 图片处理/PDF合成 | 预览拼图块 → 合成 PDF |
| Playwright | 浏览器渲染 | **可选依赖**（requirements.txt 中注释），仅预览启动，Chrome→Edge降级，用完即关 |
| parsel | HTML 解析 | 搜索页面 DOM 提取 |
| FastAPI | API 框架 | uvicorn 127.0.0.1:8000 |
| pywebview | 桌面窗口 | Windows 原生窗口（可选） |
| pystray | 系统托盘 | 显示/隐藏/退出/浏览器打开 |
| watchfiles | 文件监控 | Rust 底层，毫秒级响应，线程优雅停止 |
| APScheduler | 定时任务 | 后台线程，Cron 触发 |
| psutil | 系统监控 | health API 返回 CPU/内存 |
| SQLite | 持久化 | `~/.std_scanner/std_scanner.db`，WAL 模式 |

前端构建：Tailwind CSS v4（`@tailwindcss/cli`，`npm run build:css` 编译）。

---

## 编码规范

1. **日志**：全部使用 `logging.getLogger('std_scraper')`，禁止在管线函数中使用 `print()`。CLI入口代码（`if __name__ == '__main__'`）可保留 `print()`。
2. **HTTP 请求**：全局 `http_client`（`config/settings.py`），禁止每次新建 client。包括 `notifier.py` 的通知发送也必须复用。并发下载场景用 `create_captcha_client` 创建独立 client。
3. **版本号**：只在 `version.py` 修改，禁止其他文件硬编码版本号
4. **路径**：只在 `config/paths.py` 定义，其他模块引用
5. **模块导入**：禁止在函数内 `import` 已在文件顶部导入的模块（如 `re`、`json`）
6. **线程安全**：全局可变状态必须加锁保护（如 `_progress_lock` for `save_progress`）
7. **async 函数**：同步 HTTP 调用在 async 上下文中必须用 `run_in_executor` 包装，避免阻塞事件循环
8. **数据库 row_factory**：查询函数修改后必须 save/restore
9. **浏览器**：仅预览启动，用完立即关闭
10. **并发**：默认 `concurrent=1`，增加可能触发反爬；上限 10
11. **任务状态流转**：`running → paused → running | completed | failed | interrupted`（程序异常退出时 running/paused → interrupted）
12. **文件名**：Windows 路径限制 260 字符，`make_filename` 已做截断（150 字符）
13. **延迟**：用 `get_delay()` 函数读取配置，不要直接引用 `DELAY` 常量（启动时快照，无热更新）
14. **退出流程**：`main.py` 必须在 `os._exit(0)` 前显式调用 `close_captcha_clients()` 和 `http_client.close()`；uvicorn 必须配置 `timeout_graceful_shutdown=5`

---

## 常见坑和注意事项

- **同步函数在 async 中**：hb/db 扫描是同步函数，在路由中必须用 `run_in_executor(None, lambda: ...)` 包装
- **http_client 生命周期**：不要每次请求新建，不要调用 `.close()`（lifespan 和 main.py 退出时统一关闭）
- **CORS 不是 `*`**：只允许 127.0.0.1:8000 和 localhost:8000
- **keyword_group 默认 '安全生产'**：所有扫描端点都有此参数，前端可能传 '默认' 或 '安全生产'
- **老 API `/api/keywords` PUT**：接受的是 dict `{"keywords": [...]}`，内部保持 dict 结构而非覆写为 list
- **checkpoint 格式**：gb 用 `first_id`+`first_code`，hb/db 用 `first_pk`+`first_code`+`first_name`+`page`
- **HB/DB 下载不走验证码**：直接 GET `/portal/download/{pk}`，函数名 `download_hb_with_captcha` 仅为向后兼容
- **DB 状态兼容**：DB 网站不支持 '即将实施'（自动回退 '现行'）；HB/DB '有更新版' → '现行'
- **去重**：`dedup.py` 已从 notifier.py 独立出来，文件监控功能也在此
- **定时任务**：主循环未就绪时不再创建独立事件循环，直接标记任务失败 + 通知用户。所有类型强制 `incr=True`。扫描结果会自动持久化并下载
- **联合扫描**：`max_results` 每种类型各自拿这么多，不是按类型数均分；通过 `asyncio.gather` 并发执行
- **数据库 row_factory**：查询函数修改后必须 save/restore，否则同一线程后续调用会受影响
- **端口冲突**：启动前检测并杀掉占用 8000 端口的同名 python 进程（命令行含 `main.py`/`app.server`/`std-scanner`），避免误杀 IDE
- **中断任务清理**：`mark_interrupted` 补 `end_time`，确保 `cleanup_completed` 能按时间清理，避免无限累积
- **任务列表排序**：`get_all` 按 `created_at` 倒序，新任务排在最前
- **Windows PyInstaller**：`build.py` 显式 `--add-data` 收集 ddddocr 的 onnx 模型；Playwright 需用户手动安装

---

## 修改记录（最近版本）

### v1.1.0 — 当前版本

- **增量扫描全面优化**：GB 改为逐条 `row.id` 精确比对，HB/DB 改为逐条 `pk` 比对，遇到上次位置立即停止（不再扫满 max_results）
- **任务列表排序**：`get_all` 按 `created_at` 倒序，新任务排在最前
- **任务清理**：中断任务自动补 `end_time`，cleanup 回退到 `start_time`，避免无限累积
- **下载并发**：支持 `config.download.concurrent`（上限 10），每任务独立 httpx.Client 避免验证码 session 串扰
- **下载模块拆分**：新增 `download_helpers.py`，跨 gb/hb/db/search/quick 复用按钮检测 + hcno 提取 + PDF 落盘
- **GB 下载阶段合并 hcno 提取**：进度条从扫描完成持续移动，不再有独立"提取链接"卡顿
- **HB/DB 下载移除误用的验证码流程**：直接 GET `/portal/download/{pk}`，新增 `CopyrightError` 异常
- **状态筛选统一**：前端 4 个扫描表单统一为 现行/即将实施/废止/全部；后端处理跨网站兼容
- **SSE 增强**：`_broadcast_sse` 动态注入 duration；启动时 `sse_reset` 清理残留
- **验证码识别优化**：原图识别优先 + Otsu 自适应阈值，替代固定阈值 140
- **资源清理**：`main.py` 退出前显式 `close_captcha_clients()` + `http_client.close()`，uvicorn 配置 `timeout_graceful_shutdown=5`
- **端口冲突修复**：启动前杀掉占用 8000 端口的同名 python 进程
- **配置字段规范化**：通知渠道字段名修正（serverchan.sckey / pushplus.token / wecom.webhook）；download.preview_quality 默认 0.6

### v1.0.0 — 初始版本

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
