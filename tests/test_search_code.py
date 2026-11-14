"""标准编号解析与 GB/GB/T 纠错测试"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.scanner.search import (
    alternate_code_prefix, parse_standard_code, other_year_versions, site_uncovered_note)


class TestAlternateCodePrefix:
    def test_gb_to_gbt(self):
        assert alternate_code_prefix('GB 15831-2023') == 'GB/T 15831-2023'
        assert alternate_code_prefix('GB15831-2023') == 'GB/T 15831-2023'

    def test_gbt_to_gb(self):
        assert alternate_code_prefix('GB/T 5907.1-2014') == 'GB 5907.1-2014'

    def test_without_year(self):
        assert alternate_code_prefix('GB 15831') == 'GB/T 15831'

    def test_industry_and_local_prefix(self):
        assert alternate_code_prefix('HB 1234-2020') == 'HB/T 1234-2020'
        assert alternate_code_prefix('HB/T 1234-2020') == 'HB 1234-2020'
        assert alternate_code_prefix('DB11/T 123-2020') == 'DB11 123-2020'
        assert alternate_code_prefix('JGJ 59-2011') == 'JGJ/T 59-2011'
        assert alternate_code_prefix('CJJ/T 1-2009') == 'CJJ 1-2009'

    def test_no_t_variant_prefixes(self):
        assert alternate_code_prefix('T/CECS 1-2020') == ''
        assert alternate_code_prefix('JJF 1234-2010') == ''
        assert alternate_code_prefix('GB/Z 123-2020') == ''


class TestCompactParse:
    def test_compact(self):
        assert parse_standard_code('GB50016-2014') == {
            'prefix': 'GB', 'part': '50016', 'year': '2014'}


class TestSiteCoverage:
    def test_uncovered_family(self):
        note = site_uncovered_note('GBZ/T 325-2022')
        assert '不是同一体系' in note and '卫健委' in note
        assert site_uncovered_note('GB/T 325-2022') == ''
        assert site_uncovered_note('GB/Z 325-2022') == ''

    def test_other_year_versions(self):
        items = [{'code': 'GB/T 1234-2024', 'status': '现行'},
                 {'code': 'GB/Z 1234-2024', 'status': '现行'}]
        assert other_year_versions('GB/T 1234-2015', items) == ['GB/T 1234-2024（现行）']
        assert other_year_versions('GB/T 1234', items) == []
