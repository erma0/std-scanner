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
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

from config.paths import STATIC_DIR, UI_FILE
from app.helpers import get_logger
# 注意：app.dedup（含 watchfiles）导入耗时 0.5s+，延迟到 lifespan 内导入
from app.notifier import get_notification_service
from app.managers import task_manager, scheduler_manager

# === 必须在路由模块导入之前初始化 state ===
# 子路由模块 import 时执行 `from .state import task_manager` 会按值快照，
# 因此必须先调用 state.init_state() 再导入子路由。
# 详见 state.py 模块文档。
from app.routes import state
state.init_state(task_manager, scheduler_manager, get_notification_service())

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

# 保存 lifespan 后台任务引用，防止被 GC 中断
_background_tasks: set = set()


def _spawn_background_task(coro):
    """创建后台任务并保留引用，完成后自动从集合中移除"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


# 服务端常量
from config.settings import SERVER_PORT as _SERVER_PORT
_SERVER_ORIGINS = [f"http://localhost:{_SERVER_PORT}", f"http://127.0.0.1:{_SERVER_PORT}", "file://", "null"]


def setup_pywebview(window, loop=None):
    """注入 pywebview 窗口对象到 API 模块。

    loop 参数已废弃（lifespan 启动时会用 running loop 覆盖 state._main_loop），
    保留参数仅为兼容旧调用方，传 None 即可。
    """
    global pywebview_window
    pywebview_window = window
    state.setup_pywebview(window, loop)


# 模块级默认日志（统一使用 std_scraper logger）
logger = get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.routes.scheduled import init_config_logger, run_scheduled_scan
    from app.routes.sse import sse_reset
    init_config_logger()
    sse_reset()  # 清理上次运行残留（测试/热重载场景必需）
    logger.info(f"{APP_NAME} API v{VERSION} 启动")

    global _main_loop
    _main_loop = asyncio.get_running_loop()
    state._main_loop = _main_loop

    # 后台异步执行：所有非关键启动任务，不阻塞 lifespan yield
    # 这些任务在窗口 loading 动画期间完成，避免延长 API 就绪时间
    async def _delayed_background_tasks():
        # 1. 标记上次中断的 running/paused 任务（SQLite UPDATE，可能多条）
        await asyncio.to_thread(task_manager.mark_interrupted)

        # 2. 清理旧任务（多次 SQLite DELETE，可能慢）
        def _cleanup_tasks():
            try:
                from config.manager import load_config
                cfg = load_config()
                tc = cfg.get('tasks', {})
                retention = tc.get('retention_hours', 168)
                max_tasks = tc.get('max_tasks', 200)
                task_manager.cleanup_completed(max_age_hours=retention, max_tasks=max_tasks)
            except Exception as e:
                logger.warning(f"加载任务保留策略失败，使用默认清理: {e}")
                task_manager.cleanup_completed()
        await asyncio.to_thread(_cleanup_tasks)

        # 3. 清理 30 天前的旧通知记录
        def _cleanup_notifications():
            try:
                import app.database as _db
                _db.cleanup_notification_logs(max_age_days=30)
            except Exception as e:
                logger.warning(f"清理通知日志失败: {e}")
        await asyncio.to_thread(_cleanup_notifications)

        # 4. 启动文件监控（首次导入 watchfiles 较慢，约 0.5s，放在此异步任务里不阻塞 API 就绪）
        from app.dedup import start_file_watcher
        await asyncio.to_thread(start_file_watcher)

        # 5. 启动定时任务调度器 + 加载 jobs
        def _start_scheduler():
            scheduler_manager.start()
            if scheduler_manager.available:
                jobs = scheduler_manager.load_jobs()
                for job_id, job_config in jobs.items():
                    if job_config.get('enabled', False):
                        scheduler_manager.add_job(job_id, job_config, run_fn=run_scheduled_scan)
        await asyncio.to_thread(_start_scheduler)

    _spawn_background_task(_delayed_background_tasks())

    yield

    # 关闭时取消未完成的后台启动任务，避免与清理流程竞态
    for task in list(_background_tasks):
        if not task.done():
            task.cancel()
    # 等待取消完成（避免 event loop 关闭时产生 CancelledError 日志）
    if _background_tasks:
        await asyncio.gather(*_background_tasks, return_exceptions=True)
    _background_tasks.clear()

    from app.routes.sse import sse_close_all
    sse_close_all()
    scheduler_manager.shutdown()
    # dedup 模块延迟导入：若启动后立即关闭（用户秒退），可能尚未导入，需安全处理
    try:
        from app.dedup import stop_file_watcher
        stop_file_watcher()
    except Exception as e:
        logger.warning(f"停止文件监控失败（可能未启动）: {e}")
    task_manager.save_all()
    try:
        from config.settings import http_client, close_captcha_clients
        close_captcha_clients()
        http_client.close()
    except Exception as e:
        logger.warning(f"关闭 HTTP 客户端失败: {e}")
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
    ico = STATIC_DIR / "icon.ico"
    if ico.exists():
        return FileResponse(ico, media_type="image/x-icon")
    return HTMLResponse(status_code=404)


# 挂载静态文件（注册 SVG MIME 类型以兼容 Windows）
try:
    mimetypes.add_type('image/svg+xml', '.svg')
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
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
    if UI_FILE.exists():
        return HTMLResponse(UI_FILE.read_text(encoding='utf-8'))
    return HTMLResponse("<h1>标准速递</h1><p>ui.html 未找到</p>")


@app.get("/api/ready")
async def ready_check():
    """轻量级就绪检查：仅返回服务就绪状态，不做数据库查询。

    供 pywebview loading.html / main.py 的 wait_for_api 轮询使用，
    避免与 lifespan 后台任务的 SQLite 写入竞争，最大化启动速度。
    """
    return {"status": "ready", "version": VERSION, "app_name": APP_NAME}


@app.get("/api/health")
async def health_check():
    db_ok = True
    db_count = 0
    try:
        # 不在每次健康检查都调 ensure_db()（启动时 TaskManager 已初始化）；
        # count_tasks 在表缺失/DB 损坏时会抛异常，由这里捕获并标记 db_ok=False
        import app.database as database
        db_count = database.count_tasks()
    except Exception as e:
        logger.warning(f"健康检查：数据库查询失败: {e}")
        db_ok = False

    try:
        from app.dedup import get_dedup_stats
        dedup_stats = get_dedup_stats()
    except Exception as e:
        logger.warning(f"健康检查：获取去重状态失败: {e}")
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
