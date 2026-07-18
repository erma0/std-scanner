"""
任务管理器和调度管理器
封装全局变量，提供线程安全的任务管理接口
"""
import time
import threading
from typing import Optional, Dict, List

from config.manager import load_config, save_config
from app.helpers import get_logger
import app.database as database

logger = get_logger()


class TaskManager:
    """线程安全的任务管理器（SQLite 单存储）"""

    def __init__(self):
        database.ensure_db()
        sqlite_tasks = {}
        try:
            for t in database.get_all_tasks():
                sqlite_tasks[t['task_id']] = t
        except Exception as e:
            logger.warning(f"SQLite 加载任务失败: {e}")
        self._tasks: Dict[str, Dict] = sqlite_tasks
        self._lock = threading.Lock()

    def get(self, task_id: str) -> Optional[Dict]:
        with self._lock:
            task = self._tasks.get(task_id)
            return dict(task) if task else None

    def get_all(self, status_filter: Optional[str] = None) -> List[Dict]:
        with self._lock:
            if status_filter:
                items = [dict(t) for t in self._tasks.values() if t.get('status') == status_filter]
            else:
                items = [dict(t) for t in self._tasks.values()]
        # 按创建时间倒序：新任务排在最前。
        # 优先用 created_at（SQLite 持久化字段），缺失时回退到 start_time
        # （新建任务尚未重新加载自 DB 时只有 start_time）。
        items.sort(key=lambda t: t.get('created_at') or t.get('start_time') or 0, reverse=True)
        return items

    def create(self, task_id: str, task_data: Dict) -> Dict:
        with self._lock:
            self._tasks[task_id] = task_data
            self._persist_locked(task_id)
        return task_data

    def _broadcast_sse(self, task_snapshot: dict):
        """SSE 广播任务状态（std_items 仅保留长度信息，不推送完整列表）"""
        if 'std_items' in task_snapshot and isinstance(task_snapshot['std_items'], list):
            task_snapshot['std_items_count'] = len(task_snapshot['std_items'])
            task_snapshot['std_items'] = None
        # 动态注入 duration 字段，避免前端依赖 Date.now() 重算（暂停状态下不计入新增时长）
        start_time = task_snapshot.get('start_time')
        if start_time:
            end_time = task_snapshot.get('end_time')
            if end_time is None and task_snapshot.get('status') == 'running':
                end_time = time.time()
            if end_time:
                paused_dur = task_snapshot.get('paused_duration', 0) or 0
                if task_snapshot.get('status') == 'paused':
                    paused_at = task_snapshot.get('paused_at')
                    if paused_at:
                        paused_dur += max(0, time.time() - paused_at)
                task_snapshot['duration'] = max(0, end_time - start_time - paused_dur)
        try:
            from app.routes.sse import sse_broadcast
            sse_broadcast("task_update", task_snapshot)
        except Exception as e:
            logger.debug(f"SSE 广播失败（不影响主流程）: {e}")

    def update(self, task_id: str, status: str = None, progress: int = None,
               message: str = None, stats: Dict = None, persist_std_items: bool = True,
               **kwargs) -> bool:
        with self._lock:
            if task_id not in self._tasks:
                return False
            task = self._tasks[task_id]
            if status:
                task['status'] = status
            if progress is not None:
                task['progress'] = progress
            if message:
                task['message'] = message
            if stats:
                if 'stats' in task:
                    task['stats'].update(stats)
                else:
                    task['stats'] = stats
            for k, v in kwargs.items():
                task[k] = v

            # 仅在需要时持久化 std_items（下载阶段频繁更新时跳过，减少 SQLite 写入）
            if persist_std_items:
                self._persist_locked(task_id)
            else:
                # 轻量持久化：只更新非 std_items 字段
                self._persist_light_locked(task_id)

            task_snapshot = dict(task)

        self._broadcast_sse(task_snapshot)
        return True

    def delete(self, task_id: str) -> bool:
        """删除任务。

        直接从内存和 SQLite 删除。后台扫描/下载线程在下个 _check_pause 检查点
        会检测到 task 不存在（task_manager.get 返回 None）并自动退出，安全无副作用。
        下载中的 HTTP 请求会继续完成（PDF 文件仍写入磁盘），但 stats 无法更新（task 已删除）。
        """
        with self._lock:
            if task_id not in self._tasks:
                return False
            del self._tasks[task_id]
            try:
                database.delete_task(task_id)
            except Exception as e:
                logger.warning(f"SQLite 删除任务失败: {e}")
        return True

    def delete_all(self) -> int:
        with self._lock:
            count = len(self._tasks)
            self._tasks.clear()
        try:
            database.delete_all_tasks()
        except Exception as e:
            logger.warning(f"SQLite 清空任务失败: {e}")
        return count

    def exists(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._tasks

    def is_paused(self, task_id: str) -> bool:
        with self._lock:
            return self._tasks.get(task_id, {}).get('status') == 'paused'

    def pause(self, task_id: str) -> bool:
        with self._lock:
            if task_id not in self._tasks:
                return False
            if self._tasks[task_id].get('status') != 'running':
                return False
            self._tasks[task_id]['status'] = 'paused'
            self._tasks[task_id]['paused_at'] = time.time()
            self._tasks[task_id]['message'] = '任务已暂停'
            self._persist_locked(task_id)
            task_snapshot = dict(self._tasks[task_id])
        logger.info(f"任务已暂停: {task_id}")
        self._broadcast_sse(task_snapshot)
        return True

    def resume(self, task_id: str) -> bool:
        with self._lock:
            if task_id not in self._tasks:
                return False
            if self._tasks[task_id].get('status') != 'paused':
                return False
            task = self._tasks[task_id]
            paused_at = task.get('paused_at')
            if paused_at:
                task['paused_duration'] = task.get('paused_duration', 0) + (time.time() - paused_at)
                del task['paused_at']
            task['status'] = 'running'
            task['message'] = '任务继续执行'
            self._persist_locked(task_id)
            task_snapshot = dict(task)
        logger.info(f"任务已继续: {task_id}")
        self._broadcast_sse(task_snapshot)
        return True

    def increment_stats(self, task_id: str, **increments) -> bool:
        with self._lock:
            if task_id not in self._tasks:
                return False
            task = self._tasks[task_id]
            if 'stats' not in task:
                task['stats'] = {}
            stats = task['stats']
            for key, amount in increments.items():
                stats[key] = (stats.get(key) or 0) + amount
            self._persist_locked(task_id)
        return True

    def create_with_priority(self, task_id: str, task_data: Dict, priority: int = 0) -> Dict:
        """创建带优先级的任务。priority 越大越优先，默认 0。"""
        with self._lock:
            self._tasks[task_id] = task_data
            task_data['priority'] = priority
            self._persist_locked(task_id)
        return task_data

    def get_pending_by_priority(self) -> List[Dict]:
        """获取待执行任务，按优先级降序排列。"""
        with self._lock:
            pending = [(t.get('priority', 0), tid, t)
                      for tid, t in self._tasks.items()
                      if t.get('status') == 'pending']
            pending.sort(key=lambda x: x[0], reverse=True)
            return [dict(t) for _, _, t in pending]

    def bump_priority(self, task_id: str, delta: int = 1) -> bool:
        """提升任务优先级。delta > 0 提升，< 0 降低。"""
        with self._lock:
            if task_id not in self._tasks:
                return False
            task = self._tasks[task_id]
            task['priority'] = task.get('priority', 0) + delta
            self._persist_locked(task_id)
            task_snapshot = dict(task)
        self._broadcast_sse(task_snapshot)
        return True

    def count_by_status(self) -> Dict[str, int]:
        with self._lock:
            counts = {'total': len(self._tasks), 'running': 0, 'completed': 0,
                      'failed': 0, 'paused': 0, 'pending': 0}
            for t in self._tasks.values():
                s = t.get('status', 'pending')
                if s in counts:
                    counts[s] += 1
            return counts

    def cleanup_completed(self, max_age_hours=168, max_tasks=200):
        """清理已完成/失败的旧任务。
        
        Args:
            max_age_hours: 超过此小时数的已完成任务将被清理（默认 168h = 7天）
            max_tasks: 任务总数上限，超出时按时间删除最旧的任务
        """
        now = time.time()
        cutoff = now - max_age_hours * 3600
        with self._lock:
            keys_to_remove = []
            for k, v in self._tasks.items():
                status = v.get('status')
                if status in ('completed', 'failed', 'interrupted'):
                    # 优先用 end_time；interrupted 任务异常退出时可能无 end_time，
                    # 回退到 start_time；都没有则用 0（会被时间条件过滤掉）
                    t = v.get('end_time') or v.get('start_time') or 0
                    if t and t < cutoff:
                        keys_to_remove.append(k)

            total = len(self._tasks)
            overflow = total - max_tasks
            if overflow > 0:
                completed = [(k, v.get('end_time') or v.get('start_time') or 0)
                             for k, v in self._tasks.items()
                           if v.get('status') in ('completed', 'failed', 'interrupted')
                           and k not in keys_to_remove]
                completed.sort(key=lambda x: x[1])
                for k, _ in completed[:max(0, overflow)]:
                    if k not in keys_to_remove:
                        keys_to_remove.append(k)

            for k in keys_to_remove:
                del self._tasks[k]
                try:
                    database.delete_task(k)
                except Exception as e:
                    logger.warning(f"SQLite 删除旧任务失败: {e}")
            if keys_to_remove:
                logger.info(f"已清理 {len(keys_to_remove)} 条旧任务 (保留策略: {max_age_hours}h / max {max_tasks})")

    def mark_interrupted(self):
        """启动时将 running/paused 任务标记为 interrupted。

        程序异常退出后，running/paused 状态的任务无法继续执行，
        标记为 interrupted 后用户可在前端手动重试。
        同时补上 end_time（用程序启动时刻），确保 cleanup_completed 能按时间清理。
        """
        now = time.time()
        with self._lock:
            interrupted = []
            for k, v in self._tasks.items():
                if v.get('status') in ('running', 'paused'):
                    v['status'] = 'interrupted'
                    v['message'] = '程序异常退出，任务中断'
                    if not v.get('end_time'):
                        v['end_time'] = now
                    self._persist_locked(k)
                    interrupted.append(k)
            if interrupted:
                logger.warning(
                    f"检测到 {len(interrupted)} 个上次未完成的任务已标记为中断: {interrupted}。"
                    f"用户可在任务列表手动重试。"
                )
            return interrupted

    def save_all(self):
        """全量持久化到 SQLite（兼容接口，实际已按条目即时持久化）"""
        with self._lock:
            for task_id in self._tasks:
                self._persist_locked(task_id)

    def _persist_locked(self, task_id: str):
        """SQLite 持久化（调用方必须持有 self._lock）"""
        if task_id in self._tasks:
            try:
                database.save_task(task_id, self._tasks[task_id])
            except Exception as e:
                logger.warning(f"SQLite 同步失败: {e}")

    def _persist_light_locked(self, task_id: str):
        """轻量持久化：跳过 std_items 序列化（调用方必须持有 self._lock）

        下载阶段频繁 update 时使用，避免每 5 条就全量序列化数百条标准数据。
        std_items 会在任务完成时通过 _persist_locked 全量写入。
        """
        if task_id in self._tasks:
            try:
                database.save_task_light(task_id, self._tasks[task_id])
            except Exception as e:
                logger.warning(f"SQLite 轻量同步失败: {e}")


class SchedulerManager:
    """定时任务调度管理器"""

    def __init__(self):
        self._scheduler = None
        self._jobs: Dict[str, Dict] = {}
        self._available = False
        self._lock = threading.Lock()
        self._dirty = False
        self._last_save_time = 0
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger
            self._BackgroundScheduler = BackgroundScheduler
            self._CronTrigger = CronTrigger
            self._available = True
        except ImportError:
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    def start(self):
        if not self._available:
            logger.info("APScheduler 未安装，定时任务功能不可用")
            return
        try:
            self._scheduler = self._BackgroundScheduler()
            self._scheduler.start()
            logger.info("定时任务调度器已启动")
        except Exception as e:
            logger.error(f"定时任务调度器启动失败: {e}")
            self._scheduler = None

    def shutdown(self):
        if self._scheduler:
            try:
                self._scheduler.shutdown(wait=False)
                logger.info("定时任务调度器已关闭")
            except Exception as e:
                logger.error(f"关闭定时任务调度器失败: {e}")

    def load_jobs(self):
        with self._lock:
            config = load_config()
            self._jobs = config.get('scheduled_jobs', {})
            return dict(self._jobs)

    def save_jobs(self):
        """立即保存定时任务到 config.json"""
        with self._lock:
            self._save_jobs_locked()

    def _save_jobs_locked(self):
        """保存实现（调用方须已持有 self._lock）"""
        config = load_config()
        config['scheduled_jobs'] = dict(self._jobs)
        save_config(config)
        self._dirty = False
        self._last_save_time = time.time()

    def _mark_dirty_locked(self):
        """标记为脏，并在距上次保存超过 5 秒时立即落盘（调用方须已持有 self._lock）。
        读写 _dirty / _last_save_time 必须在锁内，避免与并发 add/remove 竞态。
        """
        self._dirty = True
        if time.time() - self._last_save_time >= 5:
            self._save_jobs_locked()

    def add_job(self, job_id: str, job_config: Dict, run_fn=None):
        if not self._available or not self._scheduler:
            return False
        with self._lock:
            # 先校验 cron 表达式（即使任务未启用也要保证可调度），避免坏 cron 残留在内存/配置中
            cron = job_config.get('cron', '0 8 * * *')
            try:
                self._CronTrigger.from_crontab(cron)
            except Exception as e:
                logger.error(f"定时任务 cron 表达式无效: {job_id} cron={cron!r}: {e}")
                return False

            self._jobs[job_id] = job_config
            if job_config.get('enabled', False) and run_fn:
                try:
                    self._scheduler.add_job(
                        run_fn,
                        self._CronTrigger.from_crontab(cron),
                        id=job_id,
                        args=[job_id, job_config],
                        replace_existing=True
                    )
                    logger.info(f"定时任务已添加: {job_id}")
                except Exception as e:
                    # 调度失败：回滚内存中的插入，避免 GET 接口返回无法调度的僵尸任务
                    self._jobs.pop(job_id, None)
                    logger.error(f"添加定时任务失败: {job_id}, {e}")
                    return False
            self._mark_dirty_locked()
        return True

    def remove_job(self, job_id: str) -> bool:
        with self._lock:
            if job_id not in self._jobs:
                return False
            if self._scheduler:
                try:
                    self._scheduler.remove_job(job_id)
                except Exception as e:
                    logger.debug(f"移除定时任务调度失败 {job_id}: {e}")
            del self._jobs[job_id]
            self._mark_dirty_locked()
        return True

    def update_job(self, job_id: str, job_config: Dict, run_fn=None):
        self.remove_job(job_id)
        return self.add_job(job_id, job_config, run_fn)

    def get_job(self, job_id: str) -> Optional[Dict]:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def get_all_jobs(self) -> Dict:
        with self._lock:
            return dict(self._jobs)

    def get_next_run_times(self) -> Dict[str, str]:
        with self._lock:
            result = {}
            if self._scheduler:
                try:
                    for job in self._scheduler.get_jobs():
                        result[job.id] = job.next_run_time.isoformat() if job.next_run_time else None
                except Exception as e:
                    logger.debug(f"获取下次运行时间失败: {e}")
            return result

    def toggle_job(self, job_id: str, enabled: bool, run_fn=None) -> bool:
        with self._lock:
            if job_id not in self._jobs:
                return False
            self._jobs[job_id]['enabled'] = enabled
            if enabled and run_fn and self._scheduler:
                try:
                    self._scheduler.add_job(
                        run_fn,
                        self._CronTrigger.from_crontab(self._jobs[job_id].get('cron', '0 8 * * *')),
                        id=job_id,
                        args=[job_id, self._jobs[job_id]],
                        replace_existing=True
                    )
                except Exception as e:
                    logger.error(f"启用定时任务失败: {job_id}, {e}")
            elif not enabled and self._scheduler:
                try:
                    self._scheduler.remove_job(job_id)
                except Exception as e:
                    logger.debug(f"禁用定时任务调度失败 {job_id}: {e}")
            self._mark_dirty_locked()
        return True


task_manager = TaskManager()
scheduler_manager = SchedulerManager()
