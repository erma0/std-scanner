"""scanner.change_tracker — 标准变更追踪

v3.5.0: 检测标准状态变更（废止、修订），与上次扫描快照对比。
首次扫描仅保存快照，不报告变更（无可对比基线）。

PRIMARY KEY 使用 (scan_type, std_code, extra_key) 三元组，
extra_key 存储 HB 的行业代码或 DB 的省份名称，避免跨行业/省份编号重复。
"""
import time
import threading
import logging
from config.paths import DB_PATH
from app.database import _get_conn, _db_lock
from app.helpers import normalize_code

_log = logging.getLogger('std_scraper')


def _ensure_table():
    """确保 std_snapshots 表存在"""
    conn = _get_conn()
    with _db_lock:
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS std_snapshots (
                    scan_type TEXT NOT NULL,
                    std_code TEXT NOT NULL,
                    extra_key TEXT NOT NULL DEFAULT '',
                    std_name TEXT,
                    state TEXT,
                    scanned_at REAL NOT NULL,
                    PRIMARY KEY (scan_type, std_code, extra_key)
                )
            """)
            conn.commit()
        except Exception as e:
            _log.warning(f"创建快照表失败: {e}")


def _load_snapshot(scan_type: str) -> list:
    """从 SQLite 加载上次扫描快照"""
    _ensure_table()
    conn = _get_conn()
    with _db_lock:
        try:
            old_rf = conn.row_factory
            conn.row_factory = None
            try:
                rows = conn.execute(
                    "SELECT std_code, std_name, state, extra_key FROM std_snapshots WHERE scan_type = ?",
                    (scan_type,)
                ).fetchall()
            finally:
                conn.row_factory = old_rf
            return [{"stdCode": r[0], "stdName": r[1], "state": r[2], "extra_key": r[3]} for r in rows]
        except Exception as e:
            _log.warning(f"加载快照失败: {e}")
            return []


def _save_snapshot(scan_type: str, standards: list):
    """保存当前扫描快照到 SQLite（替换旧快照）"""
    _ensure_table()
    conn = _get_conn()
    with _db_lock:
        try:
            conn.execute("DELETE FROM std_snapshots WHERE scan_type = ?", (scan_type,))
            now = time.time()
            for s in standards:
                code = normalize_code(s.get('stdCode') or s.get('code', ''))
                name = s.get('stdName') or s.get('name', '')
                state = s.get('state') or s.get('status', '')
                extra_key = s.get('industry', '') or s.get('province', '') or ''
                if code:
                    conn.execute(
                        "INSERT OR REPLACE INTO std_snapshots (scan_type, std_code, extra_key, std_name, state, scanned_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (scan_type, code, extra_key, name, state, now)
                    )
            conn.commit()
        except Exception as e:
            _log.warning(f"保存快照失败: {e}")


def compare_snapshot(scan_type: str, current: list) -> dict:
    """对比当前扫描结果与上次快照，返回变更列表。

    Returns:
        {
            'new_scan': bool,   # 是否首次扫描（无基线）
            'added': list,      # 新增的标准
            'changed': list,    # 状态变更的标准
            'removed': list,    # 已删除的标准（上次有，本次无）
        }
    """
    prev = _load_snapshot(scan_type)
    if not prev:
        _save_snapshot(scan_type, current)
        return {"new_scan": True, "added": [], "changed": [], "removed": []}

    prev_map = {}
    for s in prev:
        key = (s['stdCode'], s.get('extra_key', ''))
        prev_map[key] = s

    curr_map = {}
    for s in current:
        code = normalize_code(s.get('stdCode') or s.get('code', ''))
        extra = s.get('industry', '') or s.get('province', '') or ''
        key = (code, extra)
        curr_map[key] = s

    added = []
    changed = []

    for key, s in curr_map.items():
        if key not in prev_map:
            added.append({
                'code': normalize_code(s.get('stdCode') or s.get('code', '')),
                'name': s.get('stdName') or s.get('name', ''),
            })
        else:
            old_state = prev_map[key].get('state', '')
            new_state = s.get('state') or s.get('status', '')
            if old_state and new_state and old_state != new_state:
                changed.append({
                    'code': normalize_code(s.get('stdCode') or s.get('code', '')),
                    'name': s.get('stdName') or s.get('name', ''),
                    'old_state': old_state,
                    'new_state': new_state,
                })

    removed = []
    for key, s in prev_map.items():
        if key not in curr_map:
            removed.append({
                'code': s['stdCode'],
                'name': s.get('stdName', ''),
            })

    _save_snapshot(scan_type, current)

    return {
        "new_scan": False,
        "added": added,
        "changed": changed,
        "removed": removed,
    }
