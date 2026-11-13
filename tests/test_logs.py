"""应用内日志查看辅助函数测试"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.routes.logs import _log_files, _resolve_log_file, _tail_lines


class TestLogHelpers:
    def test_list_and_sort_by_mtime(self, tmp_path):
        old = tmp_path / 'std_scraper_20260101.log'
        new = tmp_path / 'std_scraper_20260102.log'
        old.write_text('old\n', encoding='utf-8')
        new.write_text('new\n', encoding='utf-8')
        (tmp_path / 'other.txt').write_text('x', encoding='utf-8')
        os.utime(old, (time.time() - 100, time.time() - 100))

        names = [p.name for p in _log_files(tmp_path)]
        assert names == ['std_scraper_20260102.log', 'std_scraper_20260101.log']

    def test_resolve_rejects_unknown_and_traversal(self, tmp_path):
        (tmp_path / 'std_scraper_20260101.log').write_text('a\n', encoding='utf-8')
        assert _resolve_log_file('std_scraper_20260101.log', tmp_path).name == \
            'std_scraper_20260101.log'
        assert _resolve_log_file('../config.json', tmp_path) is None
        assert _resolve_log_file('std_scraper_missing.log', tmp_path) is None

    def test_resolve_default_is_latest(self, tmp_path):
        old = tmp_path / 'std_scraper_20260101.log'
        new = tmp_path / 'std_scraper_20260102.log'
        old.write_text('old\n', encoding='utf-8')
        new.write_text('new\n', encoding='utf-8')
        os.utime(old, (time.time() - 100, time.time() - 100))
        assert _resolve_log_file(None, tmp_path).name == 'std_scraper_20260102.log'

    def test_tail_lines(self, tmp_path):
        path = tmp_path / 'std_scraper_20260101.log'
        path.write_text('\n'.join(f'第{i}行' for i in range(1, 101)) + '\n',
                        encoding='utf-8')
        assert _tail_lines(path, 3) == ['第98行', '第99行', '第100行']
        assert len(_tail_lines(path, 200)) == 100

    def test_tail_empty_file(self, tmp_path):
        path = tmp_path / 'std_scraper_20260101.log'
        path.write_text('', encoding='utf-8')
        assert _tail_lines(path, 10) == []
