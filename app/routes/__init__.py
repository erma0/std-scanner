"""
标准速递 - FastAPI 后端
  - 路由按功能拆分为子模块 (routes/)
  - 验证码重试（可配置）
  - 任务持久化 + 日志系统
  - 下载限速 + 任务断点续传
  - 健康检查 + 极速去重扫描
  - 实时文件监控
"""
from version import VERSION, APP_NAME

import asyncio
import mimetypes
import time
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

from app.helpers import get_logger
from app.dedup import start_file_watcher, stop_file_watcher
from app.notifier import get_notification_service
from app.managers import task_manager, scheduler_manager

# === 必须在路由模块导入之前初始化 state ===
# 因为各路由模块在 import 时就 from .state import task_manager
# 如果 state 在 import 之后才赋值，各模块拿到的始终是 None
from app.routes import state
state.task_manager = task_manager
state.scheduler_mgr = scheduler_manager
state.ns = get_notification_service()

# 导入所有路由模块
from app.routes.scheduled import router as scheduled_router
from app.routes.scan import router as scan_router
from app.routes.tasks import router as tasks_router
from app.routes.config_routes import router as config_router
from app.routes.files import router as files_router
from app.routes.search import router as search_router
from app.routes.sse import router as sse_router
from app.routes.checkpoint import router as checkpoint_router

pywebview_window = None
_main_loop = None

# 服务端常量
from config.settings import SERVER_PORT as _SERVER_PORT
_SERVER_ORIGINS = [f"http://localhost:{_SERVER_PORT}", f"http://127.0.0.1:{_SERVER_PORT}", "file://", "null"]


def setup_pywebview(window, loop):
    global pywebview_window, _main_loop
    pywebview_window = window
    _main_loop = loop
    state.setup_pywebview(window, loop)


# 模块级默认日志（统一使用 std_scraper logger）
logger = get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.routes.scheduled import init_config_logger, run_scheduled_scan
    init_config_logger()
    logger.info(f"{APP_NAME} API v{VERSION} 启动")

    global _main_loop
    _main_loop = asyncio.get_running_loop()
    state._main_loop = _main_loop

    task_manager.mark_interrupted()
    try:
        from config.manager import load_config
        cfg = load_config()
        tc = cfg.get('tasks', {})
        retention = tc.get('retention_hours', 168)
        max_tasks = tc.get('max_tasks', 200)
        task_manager.cleanup_completed(max_age_hours=retention, max_tasks=max_tasks)
    except Exception:
        task_manager.cleanup_completed()

    # 启动时清理 30 天前的旧通知记录
    try:
        import app.database as _db
        _db.cleanup_notification_logs(max_age_days=30)
    except Exception:
        pass

    start_file_watcher()
    scheduler_manager.start()
    if scheduler_manager.available:
        jobs = scheduler_manager.load_jobs()
        for job_id, job_config in jobs.items():
            if job_config.get('enabled', False):
                scheduler_manager.add_job(job_id, job_config, run_fn=run_scheduled_scan)

    yield

    from app.routes.sse import sse_close_all
    sse_close_all()
    scheduler_manager.shutdown()
    stop_file_watcher()
    task_manager.save_all()
    try:
        from config.settings import http_client, close_captcha_clients
        close_captcha_clients()
        http_client.close()
    except Exception:
        pass
    logger.info("标准速递 API 关闭")


# ==================== App 创建 ====================

app = FastAPI(
    title="标准速递 API",
    version=VERSION,
    description="标准速递 — 安全标准扫描+下载工具",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_SERVER_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# favicon.ico 兜底路由（兼容旧浏览器 / pywebview 窗口图标）
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    from fastapi.responses import FileResponse
    ico = Path(__file__).parent.parent.parent / "static" / "icon.ico"
    if ico.exists():
        return FileResponse(ico, media_type="image/x-icon")
    return HTMLResponse(status_code=404)


# 挂载静态文件（注册 SVG MIME 类型以兼容 Windows）
try:
    mimetypes.add_type('image/svg+xml', '.svg')
    static_dir = Path(__file__).parent.parent.parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
except Exception as e:
    logger.warning(f"挂载静态文件失败: {e}")


# 注册所有路由模块
app.include_router(scheduled_router)
app.include_router(scan_router)
app.include_router(tasks_router)
app.include_router(config_router)
app.include_router(files_router)
app.include_router(search_router)
app.include_router(sse_router)
app.include_router(checkpoint_router)


# ==================== 根路由 + 健康检查 ====================

@app.get("/")
async def root_ui():
    ui_path = Path(__file__).parent.parent.parent / "ui.html"
    if ui_path.exists():
        return HTMLResponse(ui_path.read_text(encoding='utf-8'))
    return HTMLResponse("<h1>标准速递</h1><p>ui.html 未找到</p>")


@app.get("/api/health")
async def health_check():
    db_ok = True
    db_count = 0
    try:
        # 不在每次健康检查都调 ensure_db()（启动时 TaskManager 已初始化）；
        # count_tasks 在表缺失/DB 损坏时会抛异常，由这里捕获并标记 db_ok=False
        import app.database as database
        db_count = database.count_tasks()
    except Exception:
        db_ok = False

    try:
        from app.dedup import get_dedup_stats
        dedup_stats = get_dedup_stats()
    except Exception:
        dedup_stats = {}

    return {
        "status": "ok",
        "version": VERSION,
        "app_name": APP_NAME,
        "timestamp": time.time(),
        "database": {"ok": db_ok, "task_count": db_count},
        "dedup": dedup_stats,
        "scheduler": {"available": scheduler_manager.available,
                      "job_count": len(scheduler_manager.load_jobs()) if scheduler_manager.available else 0}
    }
