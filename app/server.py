"""
{APP_NAME} - FastAPI 后端入口
v{VERSION} — 路由已拆分到 app/routes/ 子包，本文件为兼容性导出层。
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
