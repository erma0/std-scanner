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
from app.dedup import ExistingFilesSet


class TestGetExistingFiles:
    def test_returns_set(self):
        result = get_existing_files(force_refresh=True)
        assert isinstance(result, set)


class TestCrossFormatMatch:
    def test_docx_blocks_pdf(self):
        files = ExistingFilesSet()
        files.add('GB/T 1234-2020 标准名称.docx')
        assert 'GBT 1234-2020 标准名称.pdf' in files
        assert 'GBT 9999-2020 其他标准.pdf' not in files

    def test_historical_naming_variant(self):
        files = ExistingFilesSet()
        files.add('TSG T7004-2012 电梯监督检验和定期检验规则--液压电梯.pdf')
        assert 'TSG T7004-2012 电梯监督检验和定期检验规则-液压电梯.pdf' in files

    def test_case_space_and_extension(self):
        files = ExistingFilesSet()
        files.add('  TSG G7002-2015 锅炉定期检验规则.PDF ')
        assert 'tsg g7002-2015 锅炉定期检验规则.pdf' in files


class TestAddToCache:
    def test_add_filename(self):
        add_to_existing_files_cache('test_add.pdf')
        # Should not raise


class TestGetDedupStats:
    def test_returns_dict(self):
        stats = get_dedup_stats()
        assert isinstance(stats, dict)
