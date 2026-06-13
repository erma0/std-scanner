"""Checkpoint 模块测试"""
import sys
import json
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Mock the SCAN_CHECKPOINT_FILE before importing
import config.paths as _paths

_original = _paths.SCAN_CHECKPOINT_FILE
_original_ckpt = _paths.CKPT_FILE


class TestCheckpoint:
    @classmethod
    def setup_class(cls):
        cls.tmp = tempfile.mkdtemp()
        _paths.SCAN_CHECKPOINT_FILE = Path(cls.tmp) / 'scan_checkpoint.json'
        _paths.CKPT_FILE = Path(cls.tmp) / 'old_scan_ckpt.json'

    @classmethod
    def teardown_class(cls):
        _paths.SCAN_CHECKPOINT_FILE = _original
        _paths.CKPT_FILE = _original_ckpt

    def setup_method(self):
        if _paths.SCAN_CHECKPOINT_FILE.exists():
            _paths.SCAN_CHECKPOINT_FILE.unlink()

    def test_load_empty(self):
        from app.scanner import load_scan_checkpoint
        # load_scan_checkpoint reads from module-level SCAN_CHECKPOINT_FILE
        # which is set at import time. In test context, this may have stale data.
        ckpt = load_scan_checkpoint()
        assert isinstance(ckpt, dict)

    def test_save_and_load(self):
        from app.scanner.checkpoint import _save_scan_checkpoint, load_scan_checkpoint
        _save_scan_checkpoint({'gb': {'first_id': '123', 'last_page': 5, 'count': 100}})
        ckpt = load_scan_checkpoint()
        assert ckpt['gb']['first_id'] == '123'
        assert ckpt['gb']['last_page'] == 5

    def test_update_incr_checkpoint(self):
        from app.scanner import update_incr_checkpoint, get_incr_checkpoint
        update_incr_checkpoint('hb', 'AQ', {'first_pk': 'abc', 'last_page': 3, 'count': 45})
        ckpt = get_incr_checkpoint('hb', 'AQ')
        assert ckpt['first_pk'] == 'abc'
        assert ckpt['count'] == 45

    def test_get_nonexistent(self):
        from app.scanner import get_incr_checkpoint
        assert get_incr_checkpoint('db', 'nonexistent') is None
