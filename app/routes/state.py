"""共享状态 — routes/ 各模块通过此文件访问全局单例

⚠️ 初始化顺序关键：
  1. `app/routes/__init__.py` 必须在导入任何子路由模块之前调用 `init_state()`
  2. 子路由模块在 import 时执行 `from .state import task_manager` 会按值快照当时的值
  3. 因此 init_state() 必须先于 `from app.routes.xxx import router` 完成
  4. pywebview_window / _main_loop 由 lifespan/setup_pywebview 后续赋值

  推荐的访问方式（懒加载，永远拿到最新值）：
      from . import state
      state.task_manager.update(...)
  而非：
      from .state import task_manager  # import 时快照，热重载后会失效

  getter 函数（get_task_manager 等）保留供外部使用，但不强制内部调用方使用。
"""

task_manager = None
scheduler_mgr = None
ns = None
_main_loop = None
pywebview_window = None


def init_state(tm, sm, nsvc):
    """集中初始化共享单例。必须在导入子路由模块之前调用。

    Args:
        tm: TaskManager 实例
        sm: SchedulerManager 实例
        nsvc: NotificationService 实例
    """
    global task_manager, scheduler_mgr, ns
    task_manager = tm
    scheduler_mgr = sm
    ns = nsvc


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
    """注入 pywebview 窗口对象。

    loop 参数已废弃（lifespan 启动时会用 running loop 覆盖 _main_loop），
    保留仅为兼容旧调用方，传 None 即可。
    """
    global pywebview_window, _main_loop
    pywebview_window = window
    # 仅当调用方真的提供了运行中的 loop 才覆盖；否则留给 lifespan 设置
    if loop is not None:
        _main_loop = loop
