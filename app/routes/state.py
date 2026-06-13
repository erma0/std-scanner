"""共享状态 — routes/ 各模块通过此文件访问全局单例"""

task_manager = None
scheduler_mgr = None
ns = None
_main_loop = None
pywebview_window = None


def _require(var, name):
    if var is None:
        raise RuntimeError(f"{name} 未初始化，请确保服务已启动")
    return var


def get_task_manager():
    return _require(task_manager, 'task_manager')


def get_scheduler_mgr():
    return _require(scheduler_mgr, 'scheduler_mgr')


def get_notification_service():
    return _require(ns, 'NotificationService')


def setup_pywebview(window, loop):
    global pywebview_window, _main_loop
    pywebview_window = window
    _main_loop = loop
