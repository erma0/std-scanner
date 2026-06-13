"""
数据库管理模块 - 使用 SQLite 存储任务和定时任务
高性能版本：使用线程本地连接 + 持久化连接
"""
import sqlite3
import json
import threading
import logging
import atexit
from datetime import datetime
from typing import Dict, List, Optional, Any

from config.paths import CONFIG_DIR, DB_PATH, migrate_old_data

_log = logging.getLogger('std_scraper')
_db_lock = threading.Lock()

# 线程本地存储：每个线程保持一个连接
_thread_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """获取当前线程的数据库连接（自动创建并保持）"""
    if not hasattr(_thread_local, 'conn') or _thread_local.conn is None:
        _thread_local.conn = sqlite3.connect(
            DB_PATH, 
            check_same_thread=True,
            timeout=10.0,
        )
        _thread_local.conn.execute('PRAGMA journal_mode=WAL')
        _thread_local.conn.execute('PRAGMA synchronous=NORMAL')
        _thread_local.conn.execute('PRAGMA cache_size=-64000')
        _thread_local.conn.execute('PRAGMA temp_store=MEMORY')
    return _thread_local.conn


def _close_conn():
    """关闭当前线程的连接（主要用于测试）"""
    if hasattr(_thread_local, 'conn') and _thread_local.conn is not None:
        try:
            _thread_local.conn.close()
        except Exception:
            pass
        _thread_local.conn = None


def init_db():
    """初始化数据库"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    
    conn = _get_conn()
    with conn:
        cursor = conn.cursor()
        
        # 创建任务表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            task_type TEXT NOT NULL,
            status TEXT NOT NULL,
            progress INTEGER DEFAULT 0,
            message TEXT,
            stats TEXT,
            sub_stats TEXT,
            start_time REAL,
            end_time REAL,
            paused_duration REAL DEFAULT 0,
            paused_at REAL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        ''')
        
        # 创建定时任务表
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS scheduled_jobs (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            job_type TEXT NOT NULL,
            cron TEXT NOT NULL,
            enabled INTEGER DEFAULT 1,
            config TEXT,
            next_run_time REAL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        ''')
        
        # 创建通知记录
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS notification_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            channel TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT,
            status TEXT NOT NULL,
            error_message TEXT,
            created_at REAL NOT NULL
        )
        ''')
        
        # 创建索引
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_tasks_type ON tasks(task_type)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_notification_logs_task_id ON notification_logs(task_id)')

        for col, col_type in [('paused_duration', 'REAL DEFAULT 0'), ('paused_at', 'REAL'),
                                ('std_items', 'TEXT'), ('keyword_group', 'TEXT'),
                                ('max_results', 'INTEGER DEFAULT 500'), ('incr', 'INTEGER DEFAULT 0'),
                                ('scan_only', 'INTEGER DEFAULT 0'), ('industries', 'TEXT'),
                                ('provinces', 'TEXT'), ('changes', 'TEXT'), ('priority', 'INTEGER DEFAULT 0')]:
            try:
                cursor.execute(f'ALTER TABLE tasks ADD COLUMN {col} {col_type}')
            except sqlite3.OperationalError:
                pass
    
    _log.info("数据库初始化完成: %s", DB_PATH)


def migrate_json_to_sqlite():
    """从旧的 JSON 迁移到 SQLite"""
    try:
        # 迁移任务
        old_tasks_path = CONFIG_DIR / "tasks.json"
        if old_tasks_path.exists():
            _log.info("正在迁移任务数据: %s", old_tasks_path)
            with open(old_tasks_path, 'r', encoding='utf-8') as f:
                old_tasks = json.load(f)
            
            for task_id, task_data in old_tasks.items():
                save_task(task_id, task_data, from_migration=True)
            
            target = CONFIG_DIR / "tasks.json.backup"
            if target.exists():
                target.unlink()  # Windows 兼容：先删除旧备份
            old_tasks_path.rename(target)
            _log.info("任务数据迁移完成，备份到 tasks.json.backup")
        
        # 迁移定时任务
        old_schedules_path = CONFIG_DIR / "scheduled_jobs.json"
        if old_schedules_path.exists():
            _log.info("正在迁移定时任务数据: %s", old_schedules_path)
            with open(old_schedules_path, 'r', encoding='utf-8') as f:
                old_schedules = json.load(f)
            
            for job_id, job_data in old_schedules.items():
                save_scheduled_job(job_id, job_data, from_migration=True)
            
            target = CONFIG_DIR / "scheduled_jobs.json.backup"
            if target.exists():
                target.unlink()
            old_schedules_path.rename(target)
            _log.info("定时任务数据迁移完成，备份到 scheduled_jobs.json.backup")
    except Exception as e:
        _log.warning("数据迁移跳过: %s", e)


def save_task(task_id: str, task_data: Dict, from_migration: bool = False):
    """保存任务（高性能版本）"""
    now = datetime.now().timestamp()
    conn = _get_conn()
    
    task_type = task_data.get('std_type', 'unknown')
    status = task_data.get('status', 'pending')
    progress = task_data.get('progress', 0)
    message = task_data.get('message', '')
    stats = json.dumps(task_data.get('stats', {}))
    sub_stats = json.dumps(task_data.get('sub_stats', {}))
    start_time = task_data.get('start_time')
    end_time = task_data.get('end_time')
    paused_duration = task_data.get('paused_duration', 0)
    paused_at = task_data.get('paused_at')
    std_items = json.dumps(task_data.get('std_items', []))
    keyword_group = task_data.get('keyword_group', '')
    max_results = task_data.get('max_results', 500)
    incr = 1 if task_data.get('incr') else 0
    scan_only = 1 if task_data.get('scan_only') else 0
    industries = json.dumps(task_data.get('industries')) if task_data.get('industries') else None
    provinces = json.dumps(task_data.get('provinces')) if task_data.get('provinces') else None
    changes = json.dumps(task_data.get('changes')) if task_data.get('changes') else None
    priority = task_data.get('priority', 0)
    
    with _db_lock, conn:
        cursor = conn.cursor()
        
        cursor.execute('SELECT id FROM tasks WHERE id = ?', (task_id,))
        exists = cursor.fetchone() is not None
        
        if exists:
            cursor.execute('''
            UPDATE tasks 
            SET task_type=?, status=?, progress=?, message=?, stats=?, sub_stats=?,
                start_time=?, end_time=?, paused_duration=?, paused_at=?,
                std_items=?, keyword_group=?, max_results=?, incr=?, scan_only=?,
                industries=?, provinces=?, changes=?, priority=?, updated_at=?
            WHERE id=?
            ''', (
                task_type, status, progress, message, stats, sub_stats,
                start_time, end_time, paused_duration, paused_at,
                std_items, keyword_group, max_results, incr, scan_only,
                industries, provinces, changes, priority, now, task_id
            ))
        else:
            cursor.execute('''
            INSERT INTO tasks (
                id, task_type, status, progress, message, stats, sub_stats,
                start_time, end_time, paused_duration, paused_at,
                std_items, keyword_group, max_results, incr, scan_only,
                industries, provinces, changes, priority, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                task_id, task_type, status, progress, message, stats, sub_stats,
                start_time, end_time, paused_duration, paused_at,
                std_items, keyword_group, max_results, incr, scan_only,
                industries, provinces, changes, priority, now, now
            ))


def save_task_light(task_id: str, task_data: Dict):
    """轻量保存任务：跳过 std_items 序列化，仅更新进度/状态等轻量字段。

    用于下载阶段频繁 update 时，避免每 5 条就全量序列化数百条标准数据。
    std_items 会在任务完成时通过 save_task 全量写入。
    """
    now = datetime.now().timestamp()
    conn = _get_conn()

    task_type = task_data.get('std_type', 'unknown')
    status = task_data.get('status', 'pending')
    progress = task_data.get('progress', 0)
    message = task_data.get('message', '')
    stats = json.dumps(task_data.get('stats', {}))
    sub_stats = json.dumps(task_data.get('sub_stats', {}))
    start_time = task_data.get('start_time')
    end_time = task_data.get('end_time')
    paused_duration = task_data.get('paused_duration', 0)
    paused_at = task_data.get('paused_at')
    keyword_group = task_data.get('keyword_group', '')
    max_results = task_data.get('max_results', 500)
    incr = 1 if task_data.get('incr') else 0
    scan_only = 1 if task_data.get('scan_only') else 0
    industries = json.dumps(task_data.get('industries')) if task_data.get('industries') else None
    provinces = json.dumps(task_data.get('provinces')) if task_data.get('provinces') else None
    changes = json.dumps(task_data.get('changes')) if task_data.get('changes') else None
    priority = task_data.get('priority', 0)

    with _db_lock, conn:
        cursor = conn.cursor()
        cursor.execute('''
        UPDATE tasks
        SET task_type=?, status=?, progress=?, message=?, stats=?, sub_stats=?,
            start_time=?, end_time=?, paused_duration=?, paused_at=?,
            keyword_group=?, max_results=?, incr=?, scan_only=?,
            industries=?, provinces=?, changes=?, priority=?, updated_at=?
        WHERE id=?
        ''', (
            task_type, status, progress, message, stats, sub_stats,
            start_time, end_time, paused_duration, paused_at,
            keyword_group, max_results, incr, scan_only,
            industries, provinces, changes, priority, now, task_id
        ))


def get_task(task_id: str) -> Optional[Dict]:
    """获取单个任务"""
    conn = _get_conn()
    with _db_lock:
        orig_factory = conn.row_factory
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM tasks WHERE id = ?', (task_id,))
            row = cursor.fetchone()
            if row:
                return _row_to_task_dict(row)
        finally:
            conn.row_factory = orig_factory
    return None


def get_all_tasks(status_filter: Optional[str] = None) -> List[Dict]:
    """获取所有任务，支持按状态筛选"""
    conn = _get_conn()
    with _db_lock:
        orig_factory = conn.row_factory
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            if status_filter:
                cursor.execute('SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC', (status_filter,))
            else:
                cursor.execute('SELECT * FROM tasks ORDER BY created_at DESC')
            rows = cursor.fetchall()
            return [_row_to_task_dict(row) for row in rows]
        finally:
            conn.row_factory = orig_factory


def count_tasks() -> int:
    """获取任务总数"""
    conn = _get_conn()
    with _db_lock:
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM tasks')
            row = cursor.fetchone()
            return row[0] if row else 0
        except Exception:
            return 0


def delete_task(task_id: str) -> bool:
    """删除任务"""
    conn = _get_conn()
    with _db_lock, conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
        return cursor.rowcount > 0


def delete_all_tasks() -> int:
    """删除所有任务"""
    conn = _get_conn()
    with _db_lock, conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM tasks')
        return cursor.rowcount


def save_scheduled_job(job_id: str, job_data: Dict, from_migration: bool = False):
    """保存定时任务"""
    now = datetime.now().timestamp()
    conn = _get_conn()
    
    name = job_data.get('name', '')
    job_type = job_data.get('type', 'gb')
    cron = job_data.get('cron', '0 8 * * *')
    enabled = 1 if job_data.get('enabled', True) else 0
    config = json.dumps(job_data.get('config', {}))
    next_run_time = job_data.get('next_run_time')
    
    with _db_lock, conn:
        cursor = conn.cursor()
        
        # 检查是否存在
        cursor.execute('SELECT id FROM scheduled_jobs WHERE id = ?', (job_id,))
        exists = cursor.fetchone() is not None
        
        if exists:
            cursor.execute('''
            UPDATE scheduled_jobs 
            SET name=?, job_type=?, cron=?, enabled=?, config=?, 
                next_run_time=?, updated_at=?
            WHERE id=?
            ''', (
                name, job_type, cron, enabled, config, next_run_time, now, job_id
            ))
        else:
            cursor.execute('''
            INSERT INTO scheduled_jobs (
                id, name, job_type, cron, enabled, config,
                next_run_time, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                job_id, name, job_type, cron, enabled, config,
                next_run_time, now, now
            ))


def get_all_scheduled_jobs() -> List[Dict]:
    """获取所有定时任务"""
    conn = _get_conn()
    with _db_lock:
        orig_factory = conn.row_factory
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM scheduled_jobs ORDER BY created_at DESC')
            rows = cursor.fetchall()
            return [_row_to_schedule_dict(row) for row in rows]
        finally:
            conn.row_factory = orig_factory


def delete_scheduled_job(job_id: str) -> bool:
    """删除定时任务"""
    conn = _get_conn()
    with _db_lock, conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM scheduled_jobs WHERE id = ?', (job_id,))
        return cursor.rowcount > 0


def log_notification(task_id: Optional[str], channel: str, title: str, 
                     content: str, status: str, error_message: Optional[str] = None):
    """记录通知发送记录"""
    now = datetime.now().timestamp()
    conn = _get_conn()
    
    with _db_lock, conn:
        cursor = conn.cursor()
        cursor.execute('''
        INSERT INTO notification_logs (
            task_id, channel, title, content, status, error_message, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (task_id, channel, title, content, status, error_message, now))


def get_notification_logs(task_id: str) -> List[Dict]:
    """获取任务的通知记录"""
    conn = _get_conn()
    with _db_lock:
        orig_factory = conn.row_factory
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute('''
            SELECT * FROM notification_logs WHERE task_id = ? ORDER BY created_at DESC
            ''', (task_id,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.row_factory = orig_factory


def cleanup_notification_logs(max_age_days: int = 30) -> int:
    """清理超过指定天数的旧通知记录"""
    cutoff = datetime.now().timestamp() - max_age_days * 86400
    conn = _get_conn()
    with _db_lock, conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM notification_logs WHERE created_at < ?', (cutoff,))
        deleted = cursor.rowcount
        if deleted > 0:
            _log.info(f"已清理 {deleted} 条超过 {max_age_days} 天的通知记录")
        return deleted


def _row_to_task_dict(row: sqlite3.Row) -> Dict:
    """将数据库行转换为任务字典。数据库列名 'id' 映射为内存 'task_id'。"""
    task = dict(row)
    if 'id' in task and 'task_id' not in task:
        task['task_id'] = task['id']
    if 'paused_duration' not in task:
        task['paused_duration'] = 0
    _json_load(task, 'stats')
    _json_load(task, 'sub_stats')
    _json_load(task, 'std_items')
    _json_load(task, 'changes')
    _json_load(task, 'industries')
    _json_load(task, 'provinces')
    if 'incr' in task:
        task['incr'] = bool(task['incr'])
    if 'scan_only' in task:
        task['scan_only'] = bool(task['scan_only'])
    return task


def _json_load(task: Dict, key: str):
    """安全反序列化 JSON 字段"""
    try:
        if task.get(key) and isinstance(task[key], str):
            task[key] = json.loads(task[key])
    except (json.JSONDecodeError, TypeError):
        pass


def _row_to_schedule_dict(row: sqlite3.Row) -> Dict:
    """将数据库行转换为定时任务字典"""
    job = dict(row)
    if job.get('config'):
        job['config'] = json.loads(job['config'])
    job['enabled'] = bool(job['enabled'])
    return job


_db_initialized = False
_migration_done = False
_migration_lock = threading.Lock()


def ensure_db():
    """确保数据库已初始化（延迟初始化，避免模块导入副作用）"""
    global _db_initialized, _migration_done
    if _db_initialized and _migration_done:
        return
    with _db_lock:
        if not _db_initialized:
            migrate_old_data()          # 自动迁移旧路径数据
            init_db()
            _db_initialized = True
    # 迁移 JSON → SQLite 在 _db_lock 外执行（save_task 内部会获取 _db_lock）
    # 用独立 _migration_lock 防止多线程重复迁移
    if not _migration_done:
        with _migration_lock:
            if not _migration_done:
                try:
                    migrate_json_to_sqlite()
                except Exception as e:
                    _log.warning("JSON→SQLite 迁移失败: %s", e)
                _migration_done = True


# 注册 atexit 回调清理连接
atexit.register(_close_conn)
