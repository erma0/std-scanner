# 标准速递

基于全国标准信息公共服务平台 (std.samr.gov.cn) 的安全标准批量扫描与下载工具，支持国家标准、行业标准、地方标准三类。

> AI 开发文档见 [`AGENTS.md`](AGENTS.md)，项目长期记忆见 [`.workbuddy/memory/MEMORY.md`](.workbuddy/memory/MEMORY.md)。

## 功能特性

- **三类标准全覆盖**：国家标准 / 行业标准 / 地方标准，支持联合扫描
- **智能去重**：os.scandir 高速扫描 + 多线程并行 + watchfiles 实时监控
- **验证码自动识别**：ddddocr 驱动，自动重试 5-8 次
- **预览转 PDF**：拼图块自动合成，无需人工操作
- **桌面应用**：pywebview 窗口 + 系统托盘，支持最小化到托盘
- **WebUI 界面**：现代化 Web 界面，支持搜索、扫描、任务管理、通知配置
- **多渠道通知**：Server酱 / PushPlus / 企业微信 / 钉钉
- **定时任务**：Cron 表达式定时扫描，支持启用/禁用/手动触发
- **任务控制**：支持暂停/继续/重试，进度实时反馈

## 快速开始

### 安装依赖

```bash
pip install -r requirements.txt
playwright install chromium
```

### 启动

```bash
# 桌面应用（推荐）
python main.py

# 仅启动 API 服务
python -m app.server
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
| `--pages=N` | 采集条数（每页 50 条，自动计算页数） | 500 |
| `--scan-only` | 只扫描不下载 | — |
| `--dl-only` | 仅从已有数据下载 | — |
| `--incr` | 增量模式（断点续扫） | — |
| `--search=关键词` | 搜索下载（内部 API） | — |
| `--search-web=关键词` | 搜索下载（检索网站） | — |
| `--scan-hb[=行业]` | 扫描行业标准 | 安全相关 |
| `--scan-db[=省份]` | 扫描地方标准 | 江苏省 |
| `--type=类型` | 标准类型 | 国家标准 |
| `--max=N` | 搜索最大数量 | 5 |
| `--delay=N` | 请求间隔（秒） | 3 |
| `--output-dir=PATH` | PDF 存放路径 | ~/Downloads/安全标准 |

## 标准类型下载说明

| 标准类型 | 可下载 | 说明 |
|---------|--------|------|
| 国家标准 | ✅ | 公开 PDF 下载，支持预览转 PDF |
| 行业标准 | ✅ | 验证码下载，支持 AQ/XF/GA 等行业 |
| 地方标准 | ✅ | 验证码下载，支持各省地方标准 |
| 国家标准计划 | ❌ | 仅计划信息，无 PDF |

## 通知配置

支持以下通知渠道，在 WebUI 中配置：

| 渠道 | 说明 |
|------|------|
| Server酱 | 微信推送 (sctapi.ftqq.com) |
| PushPlus | 微信推送 (pushplus.plus) |
| 企业微信 | Webhook 机器人推送 |
| 钉钉 | Webhook + Secret 签名 |

## 配置文件

`~/.std_scanner/config.json`，支持热更新：

```json
{
  "download": {
    "output_dir": "~/Downloads/安全标准",
    "delay": 3.0,
    "concurrent": 1,
    "max_retries": 3
  },
  "notifications": {
    "serverchan": { "enabled": false, "sckey": "" },
    "wecom": { "enabled": false, "webhook": "" }
  },
  "logging": { "level": "INFO" }
}
```

## 安全关键词

覆盖 113 安全相关关键词：消防 / 化工 / 机械 / 电气 / 特种设备 / 矿山 / 建筑 / 职业卫生 / 应急 / 交通 / 防护等。

自动排除：信息安全 / 网络安全 / 数据安全 / 食品安全（内置）+ 用户自定义排除词（界面配置）

## 版本历史

| 版本 | 更新内容 |
|------|---------|
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
