"""数据库模块测试 — v3.5.0 扩展

重点验证 _row_to_task_dict 的 id → task_id 映射，
以及 std_snapshots 表的创建。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import app.database as database


class TestDatabaseInit:
    def test_ensure_db_creates_tables(self):
        database.ensure_db()
        conn = database._get_conn()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = [t[0] for t in tables]
        assert 'tasks' in table_names
        assert 'scheduled_jobs' in table_names
        assert 'notification_logs' in table_names


class TestRowToTaskDict:
    def test_id_mapped_to_task_id(self):
        """验证 _row_to_task_dict 将 id 映射为 task_id"""
        database.ensure_db()
        conn = database._get_conn()
        conn.execute("DELETE FROM tasks WHERE id = 'test-map-001'")
        conn.commit()

        database.save_task(
            'test-map-001',
            {'status': 'running', 'progress': 50, 'message': 'test', 'std_type': 'gb'}
        )
        task = database.get_task('test-map-001')
        assert task is not None
        assert 'task_id' in task
        assert task['task_id'] == 'test-map-001'

        conn.execute("DELETE FROM tasks WHERE id = 'test-map-001'")
        conn.commit()

    def test_stats_parsed_from_json(self):
        """验证 stats 字段从 JSON 字符串解析为 dict"""
        database.ensure_db()
        conn = database._get_conn()
        conn.execute("DELETE FROM tasks WHERE id = 'test-stats-001'")
        conn.commit()

        database.save_task(
            'test-stats-001',
            {'status': 'completed', 'progress': 100, 'message': 'done',
             'std_type': 'gb', 'stats': {'scanned': 10, 'downloaded': 5}}
        )
        task = database.get_task('test-stats-001')
        assert task is not None
        assert isinstance(task.get('stats'), dict)
        assert task['stats']['scanned'] == 10

        conn.execute("DELETE FROM tasks WHERE id = 'test-stats-001'")
        conn.commit()


class TestTaskOperations:
    def test_create_and_get(self):
        database.ensure_db()
        conn = database._get_conn()
        conn.execute("DELETE FROM tasks WHERE id = 'test-task-001'")
        conn.commit()

        database.save_task(
            'test-task-001',
            {'status': 'running', 'progress': 50, 'message': 'test', 'std_type': 'gb'}
        )
        task = database.get_task('test-task-001')
        assert task is not None
        assert task['status'] == 'running'

        conn.execute("DELETE FROM tasks WHERE id = 'test-task-001'")
        conn.commit()

    def test_get_all_tasks(self):
        database.ensure_db()
        tasks = database.get_all_tasks()
        assert isinstance(tasks, list)


class TestStdSnapshots:
    def test_table_created(self):
        """验证 std_snapshots 表可被创建"""
        from app.scanner.change_tracker import _ensure_table
        _ensure_table()
        conn = database._get_conn()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='std_snapshots'"
        ).fetchall()
        assert len(tables) == 1
