"""批量搜索诊断记录器测试"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.scanner.search_diag import SearchBatchDiag, queries_for_retry


class TestSearchBatchDiag:
    def test_empty_site_result(self):
        d = SearchBatchDiag(2)
        reason = d.record('GB 1-2020', raw=0, site_total=0)
        assert '站点无结果' in reason
        data = d.to_dict()
        assert data['empty_queries'] == 1
        assert data['zero_samples'][0] == {
            'query': 'GB 1-2020', 'reason': reason, 'kind': 'no_result'}

    def test_unparsed_page_detected(self):
        d = SearchBatchDiag(1)
        reason = d.record('GB 1-2020', raw=0, site_total=120)
        assert '未解析出结果' in reason and '120' in reason
        assert d.zero_samples[0]['kind'] == 'unparsed'

    def test_code_mismatch_with_samples(self):
        d = SearchBatchDiag(1)
        reason = d.record('GB 1-2020', raw=3, matched=0,
                          sample_codes=['GB 2-2020', 'GB 3-2020'])
        assert '编号不匹配' in reason and 'GB 2-2020' in reason
        assert d.hit_queries == 1 and d.matched_total == 0
        assert d.zero_samples[0]['kind'] == 'mismatch'

    def test_added_reason_empty(self):
        d = SearchBatchDiag(1)
        reason = d.record('消防', raw=20, matched=10, added=3,
                          existing=2, duplicate=5, unavailable=1, obsolete=1)
        assert reason == ''
        assert d.added_total == 3 and d.existing_total == 2
        assert not d.zero_samples
        assert d.kind_counts == {}

    def test_error(self):
        d = SearchBatchDiag(1)
        reason = d.record('消防', error='ReadTimeout: timeout')
        assert '搜索异常' in reason and 'ReadTimeout' in reason
        assert d.error_queries == 1
        assert d.zero_samples[0]['kind'] == 'error'

    def test_kind_counts_for_skipped(self):
        d = SearchBatchDiag(3)
        d.record('q1', raw=5, matched=2, duplicate=2)
        d.record('q2', raw=5, matched=1, existing=1)
        d.record('q3', raw=5, matched=3, duplicate=1, existing=2)
        assert d.zero_samples[0]['kind'] == 'duplicate'
        assert d.zero_samples[1]['kind'] == 'existing'
        # 多原因时取数量最多的一类
        assert d.zero_samples[2]['kind'] == 'existing'
        assert d.kind_counts == {'duplicate': 1, 'existing': 2}

    def test_consecutive_empty_warning(self):
        d = SearchBatchDiag(10)
        for i in range(10):
            d.record(f'q{i}', raw=0)
        assert d.consecutive_empty == 10
        assert '连续 10 个查询' in d.warning


class TestQueriesForRetry:
    def _samples(self):
        return [
            {'query': 'GB 1-2020', 'kind': 'no_result'},
            {'query': 'gb 1-2020', 'kind': 'error'},       # 归一化后重复
            {'query': 'GB 2-2020', 'kind': 'mismatch'},
            {'query': 'GB 3-2020', 'kind': 'error'},
            {'query': '', 'kind': 'error'},
        ]

    def test_all_kinds_by_default(self):
        assert queries_for_retry(self._samples()) == [
            'GB 1-2020', 'GB 2-2020', 'GB 3-2020']

    def test_filter_by_kind(self):
        assert queries_for_retry(self._samples(), ['error']) == [
            'gb 1-2020', 'GB 3-2020']
        assert queries_for_retry(self._samples(), ['mismatch', 'no_result']) == [
            'GB 1-2020', 'GB 2-2020']

    def test_unknown_kind_no_match(self):
        assert queries_for_retry(self._samples(), ['obsolete']) == []

