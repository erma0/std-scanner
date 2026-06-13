"""去重模块测试"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import config.manager
# Mock config to avoid loading real config
config.manager.load_config = lambda: {
    'dedup': {'existing_dirs': [], 'cache_window': 600}
}

from app.dedup import get_existing_files, add_to_existing_files_cache, get_dedup_stats


class TestGetExistingFiles:
    def test_returns_set(self):
        result = get_existing_files(force_refresh=True)
        assert isinstance(result, set)


class TestAddToCache:
    def test_add_filename(self):
        add_to_existing_files_cache('test_add.pdf')
        # Should not raise


class TestGetDedupStats:
    def test_returns_dict(self):
        stats = get_dedup_stats()
        assert isinstance(stats, dict)
