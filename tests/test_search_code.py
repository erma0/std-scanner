"""标准编号解析与 GB/GB/T 纠错测试"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.scanner.search import alternate_code_prefix, parse_standard_code


class TestAlternateCodePrefix:
    def test_gb_to_gbt(self):
        assert alternate_code_prefix('GB 15831-2023') == 'GB/T 15831-2023'
        assert alternate_code_prefix('GB15831-2023') == 'GB/T 15831-2023'

    def test_gbt_to_gb(self):
        assert alternate_code_prefix('GB/T 5907.1-2014') == 'GB 5907.1-2014'

    def test_requires_year(self):
        assert alternate_code_prefix('GB 15831') == ''

    def test_non_gb_prefix_untouched(self):
        assert alternate_code_prefix('HB 1234-2020') == ''
        assert alternate_code_prefix('DB11/T 123-2020') == ''


class TestCompactParse:
    def test_compact(self):
        assert parse_standard_code('GB50016-2014') == {
            'prefix': 'GB', 'part': '50016', 'year': '2014'}
