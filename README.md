# 标准速递

基于全国标准信息公共服务平台 (std.samr.gov.cn) 的安全标准批量扫描与下载工具，支持国家标准、行业标准、地方标准三类。

> AI 开发文档见 [`AGENTS.md`](AGENTS.md)。

## 功能特性

- **三类标准全覆盖**：国家标准 / 行业标准 / 地方标准，支持联合扫描
- **状态筛选**：现行 / 即将实施 / 废止 / 全部，跨网站自动兼容
- **增量扫描**：逐条精确比对（GB 用 id、HB/DB 用 pk），遇到上次位置即停
- **智能去重**：os.scandir 高速扫描 + 多线程并行 + watchfiles 实时监控
- **验证码自动识别**：ddddocr 驱动，原图优先 + 多预处理策略 + Otsu 自适应阈值，最多 12 次重试
- **预览转 PDF**：拼图块自动合成，无需人工操作（需安装 Playwright）
- **下载并发**：可配置 1-10 并发，每任务独立 HTTP client 避免验证码 session 串扰
- **桌面应用**：pywebview 窗口 + 系统托盘，支持最小化到托盘
- **WebUI 界面**：现代化 Web 界面，支持搜索、扫描、任务管理、通知配置、任务搜索筛选
- **多渠道通知**：Server酱 / PushPlus / 企业微信 / 钉钉
- **定时任务**：Cron 表达式定时扫描，支持启用/禁用/手动触发
- **任务控制**：支持暂停/继续/重试/单条重试/批量重试失败项/优先级插队，进度实时反馈
- **标准变更追踪**：自动对比上次扫描结果，标记新增/变更/移除

## 快速开始

### 安装依赖

```bash
# 核心依赖
pip install -r requirements.txt

# 浏览器预览转PDF（可选功能，不装则仅支持直接下载）
playwright install chromium
```

### 启动

```bash
# 桌面应用（推荐）
python main.py

# 仅启动 API 服务（不依赖 pywebview，可浏览器访问）
python -m uvicorn app.server:app --host 127.0.0.1 --port 8000
# 浏览器访问 http://127.0.0.1:8000
```

### 命令行使用

```bash
# 搜索并下载
python -m app.scanner.quick --search-web=消火栓
python -m app.scanner.quick --search-web=化工 --type=行业标准

# 国家标准批量扫描
python -m app.scanner.quick --pages=10              # 扫描 10 页+下载
python -m app.scanner.quick --pages=50 --scan-only  # 只扫描不下载
python -m app.scanner.quick --pages=1000 --incr     # 增量模式

# 行业标准扫描
python -m app.scanner.quick --scan-hb               # 默认安全相关
python -m app.scanner.quick --scan-hb=AQ,XF         # 指定行业代码
python -m app.scanner.quick --scan-hb=all           # 全部行业

# 地方标准扫描
python -m app.scanner.quick --scan-db               # 默认江苏省
python -m app.scanner.quick --scan-db=浙江省,上海市   # 指定省份
python -m app.scanner.quick --scan-db=all           # 全部省份
```

### 命令行参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--pages=N` | 国标采集条数（每页 50 条，自动计算页数） | 500 |
| `--max-pages=N` | 扫描最大条数（HB/DB 通用） | 500 |
| `--scan-only` | 只扫描不下载 | — |
| `--dl-only` | 仅从已有 `safety_full.json` 下载 | — |
| `--incr` | 增量模式（断点续扫，遇上次位置即停） | — |
| `--search=关键词` | 搜索下载（内部 API） | — |
| `--search-web=关键词` | 搜索下载（标准检索网站） | — |
| `--scan-hb[=行业]` | 扫描行业标准 | 安全相关 |
| `--scan-db[=省份]` | 扫描地方标准 | 江苏省 |
| `--type=类型` | 标准类型，仅 `--search-web` 时生效：国家标准/行业标准/地方标准/国家标准计划 | 国家标准 |
| `--max=N` | 搜索最大数量，仅 `--search-web` 时生效 | 5 |
| `--keywords=PATH` | 从文件加载关键词（每行一个，`#` 注释） | — |
| `--delay=N` | 请求间隔（秒） | 3 |
| `--output-dir=PATH` | PDF 存放路径 | ~/Downloads/安全标准 |

> 状态筛选（现行/即将实施/废止/全部）仅在 WebUI 中支持，CLI 未暴露。

## 辅助工具

`tools/analyze_dup.py` 是独立的 PDF 目录重复分析脚本（不参与应用运行，也不被任何模块导入），用于排查已下载标准库中的重复 / 近重复文件：

```bash
# 分析指定目录（默认硬编码本地路径，建议传参）
python tools/analyze_dup.py "D:\标准规范"

# 导出结果
python tools/analyze_dup.py "D:\标准规范" --output csv   # 或 json
```

输出三类结果：精确重复（同大小+同内容）、同编号不同文件名、文件名异常（双扩展名 / 特殊连字符 / GBT 格式等）。

## 标准类型下载说明

| 标准类型 | 可下载 | 说明 |
|---------|--------|------|
| 国家标准 | ✅ | 公开 PDF 下载（验证码），支持预览转 PDF |
| 行业标准 | ✅ | 直接下载（无需验证码），AQ/XF/JG/DL/SL 可下载，YS/HG/JT 版权限制 |
| 地方标准 | ✅ | 直接下载（无需验证码） |
| 国家标准计划 | ❌ | 仅计划信息，无 PDF |

## 通知配置

支持以下通知渠道，在 WebUI 中配置：

| 渠道 | 字段 | 说明 |
|------|------|------|
| Server酱 | `sckey` | 微信推送 (sctapi.ftqq.com) |
| PushPlus | `token` | 微信推送 (pushplus.plus) |
| 企业微信 | `webhook` | Webhook 机器人推送 |
| 钉钉 | `webhook` + `secret` | Webhook + Secret 签名 |

## 配置文件

`~/.std_scanner/config.json`，支持热更新：

```json
{
  "download": {
    "output_dir": "~/Downloads/安全标准",
    "delay": 3.0,
    "concurrent": 1,
    "max_retries": 3,
    "retry_delay": 2.0,
    "allow_preview": true,
    "preview_quality": 0.6,
    "strategy": "full",
    "duplicate_check_strategy": "early"
  },
  "notifications": {
    "serverchan": {"enabled": false, "sckey": ""},
    "pushplus":   {"enabled": false, "token": ""},
    "wecom":      {"enabled": false, "webhook": ""},
    "dingtalk":   {"enabled": false, "webhook": "", "secret": ""}
  },
  "logging": {"level": "INFO", "save_to_file": true},
  "tasks": {
    "retention_hours": 168,
    "max_tasks": 200
  }
}
```

**关键配置项**：
- `download.concurrent`：下载并发度，1-10（默认 1，增大可能触发反爬）
- `download.preview_quality`：预览 PDF 质量，0.3-1.0（默认 0.6）
- `tasks.retention_hours`：任务历史保留时长，默认 168 小时（7 天）
- `tasks.max_tasks`：任务历史最大条数，默认 200

## 安全关键词

覆盖 113 安全相关关键词：消防 / 化工 / 机械 / 电气 / 特种设备 / 矿山 / 建筑 / 职业卫生 / 应急 / 交通 / 防护等。

自动排除：信息安全 / 网络安全 / 数据安全 / 食品安全 / 农产品 / 饲料（内置）+ 用户自定义排除词（界面配置）

## 版本历史

| 版本 | 更新内容 |
|------|---------|
| v1.1.0 | 增量扫描全面优化（逐条精确比对遇上次位置即停）、任务列表新任务置顶、中断任务自动清理、下载并发支持（1-10）、download_helpers 模块拆分、GB 下载合并 hcno 提取、HB/DB 移除误用验证码流程、状态筛选统一（现行/即将实施/废止/全部）、SSE 增强、验证码原图识别+Otsu 优化、退出资源清理、端口冲突修复、配置字段名规范化 |
| v1.0.0 | 完整的国标/行标/地标扫描+下载链路、安全关键词多组管理、SSE 实时推送、任务优先级、统一增量 checkpoint、预览转 PDF、多渠道通知、文件去重+实时监控、定时扫描、标准变更快照对比 |
| v3.9.1 | 性能优化(save_task_light)、GB下载重构(showGb→verifyCode→viewGb)、hcno智能提取、PDF类型验证、端口冲突修复 |
| v3.9.0 | Targeted DOM Update 消除 UI 抖动、std_items 全量持久化、清理策略可配置(时长+数量上限) |
| v3.8.0 | 下载状态实时推送(SSE)、per-item 重试/批量重试、扫描中间结果推送、预览禁用识别 |
| v3.6.1 | 品牌更名「标准速递」、修复退出CancelledError、删除限流中间件、系统设置/通知自动保存、优雅退出、去重PDF计数修复 |
| v3.6.0 | SSE修复、search.py阻塞修复、线程安全、原子写入、路径安全、任务优先级插队、Pydantic校验 |
| v3.4.3 | UI全面优化、task_manager None修复、pywebview 6.x适配、侧边栏折叠、无边框窗口 |
| v3.4.0 | 架构重构：scanner/server子包拆分、统一扫描引擎、双下载路径消除 |
| v3.3.2 | 联合扫描统一采集条数、标签修正、增量扫描默认开启 |
| v3.3.0 | 统一增量 checkpoint、按采集条数配置、关键词管理 UI |
| v3.2.2 | 配置路径迁移 ~/.std_scanner/、线程安全修复、模块拆分 |
| v3.2.1 | Bug 修复：响应码兼容、事件循环阻塞、文件名截断、logging 替换 print |
| v3.2 | SQLite 持久化、定时任务调度、任务暂停/继续/重试、联合扫描 |
| v3.1 | 去重优化（scandir + 多线程 + watchfiles）、断点续传 |
| v3.0 | FastAPI + pywebview 架构重构、WebUI、多渠道通知 |
