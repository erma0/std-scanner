"""扫描错误收集与下载统计测试"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.scanner.utils import (
    ScanErrorCollector, compute_download_stats, format_download_summary,
)


class FakeTaskManager:
    def __init__(self):
        self.updates = []

    def update(self, task_id, **kwargs):
        self.updates.append(kwargs)
        return True


class TestScanErrorCollector:
    def test_no_error(self):
        c = ScanErrorCollector()
        assert c.note() == ''
        assert c.message_suffix() == ''
        assert c.fail_message() == ''
        c.raise_if_empty([])  # 无失败时不应抛异常

    def test_page_error_note(self):
        c = ScanErrorCollector(log_prefix='[T]')
        c.callback(2, RuntimeError('timeout'))
        assert c.count == 1
        assert '1 页请求失败' in c.note()
        assert 'timeout' in c.note()

    def test_label_error(self):
        c = ScanErrorCollector()
        c.callback('江苏', 3, RuntimeError('reset'))
        assert '江苏 第3页' in c.errors[0]

    def test_reset_clears_note(self):
        tm = FakeTaskManager()
        c = ScanErrorCollector(tm, 't')
        c.callback(1, RuntimeError('x'))
        c.reset()
        assert c.count == 0
        assert tm.updates[-1]['scan_note'] == ''

    def test_raise_if_empty(self):
        c = ScanErrorCollector()
        c.callback(1, RuntimeError('boom'))
        try:
            c.raise_if_empty([])
        except RuntimeError as e:
            assert '扫描失败' in str(e)
        else:
            raise AssertionError('无结果且有失败时应抛异常')
        c.raise_if_empty([{'code': 'GB 1-2020'}])  # 有结果时不抛


class TestDownloadStatsReasons:
    def test_fail_reasons_aggregated(self):
        items = [
            {'dlStatus': 'failed', 'failReason': '超时'},
            {'dlStatus': 'failed', 'failReason': '超时'},
            {'dlStatus': 'failed', 'failReason': '验证码错误'},
            {'dlStatus': 'downloaded'},
        ]
        stats = compute_download_stats(items)
        assert stats['failed'] == 3
        assert stats['fail_reasons'] == {'超时': 2, '验证码错误': 1}
        summary = format_download_summary(stats)
        assert '失败 3' in summary
        assert '超时' in summary
