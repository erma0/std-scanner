"""
标准速递 - FastAPI 后端入口（兼容性导出层）

⚠️ 实际的 FastAPI app、lifespan、路由注册都在 app/routes/__init__.py 中。
   本文件仅为兼容历史导入（main.py 用 `app.server:app` 启动 uvicorn）而存在，
   以及为外部调用方提供统一入口。
   新代码应直接从 app.routes 导入。
"""
from version import VERSION, APP_NAME  # noqa: F401 — re-exported via __all__

from app.routes import app, _main_loop, pywebview_window, task_manager, scheduler_manager, setup_pywebview
from app.routes.scheduled import run_scheduled_scan, load_scheduled_jobs, save_scheduled_jobs
from config.settings import SERVER_HOST as _SERVER_HOST, SERVER_PORT as _SERVER_PORT

__all__ = [
    'app', '_main_loop', 'pywebview_window',
    '_SERVER_HOST', '_SERVER_PORT',
    'task_manager', 'scheduler_manager',
    'setup_pywebview', 'run_scheduled_scan',
    'load_scheduled_jobs', 'save_scheduled_jobs',
]
