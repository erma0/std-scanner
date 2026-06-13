"""scanner.progress — 进度保存"""

import json
import time
import threading
import logging

from config.paths import DATA_FILE
from app.helpers import atomic_write

_log = logging.getLogger('std_scraper')

_progress_last_save = 0
_progress_save_interval = 10
_progress_lock = threading.Lock()


def save_progress(standards, force=False):
    """优化的进度保存：按时间间隔保存，减少磁盘IO（线程安全，原子写入）"""
    global _progress_last_save
    now = time.time()

    if not force and (now - _progress_last_save) < _progress_save_interval:
        return

    with _progress_lock:
        if not force and (now - _progress_last_save) < _progress_save_interval:
            return
        atomic_write(str(DATA_FILE), json.dumps({
            'generatedAt': time.strftime('%Y-%m-%d %H:%M:%S'),
            'total': len(standards),
            'standards': standards,
        }, ensure_ascii=False, indent=2), mode='w')
        _progress_last_save = now
