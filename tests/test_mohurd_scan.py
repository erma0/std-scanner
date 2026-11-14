"""住建部工程建设标准公告解析测试（纯函数，不发起网络请求）"""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.scanner import mohurd_scan
from app.scanner.mohurd_scan import (
    _ART_ID_RE, _candidate_score, _compact, _extract_name, _with_space,
)
from app.scanner.search import parse_standard_code


class TestCodeHelpers:
    def test_compact(self):
        assert _compact('GB 50016-2014') == 'GB50016-2014'
        assert _compact('gb/t50145-2007') == 'GB/T50145-2007'

    def test_with_space(self):
        assert _with_space('GB51043-2014') == 'GB 51043-2014'
        assert _with_space('GB/T50145-2007') == 'GB/T 50145-2007'

    def test_compact_code_parse(self):
        assert parse_standard_code('GB50016-2014') == {
            'prefix': 'GB', 'part': '50016', 'year': '2014'}
        assert parse_standard_code('GB/T50145-2007') == {
            'prefix': 'GB/T', 'part': '50145', 'year': '2007'}
        # 带地区码的前缀仍走原规则，/T 保留在 prefix 中
        assert parse_standard_code('DB11/T 123-2020')['prefix'] == 'DB11/T'

    def test_article_id(self):
        url = 'https://www.mohurd.gov.cn/gongkai/zc/wjk/art/2015/art_17339_224333.html'
        assert _ART_ID_RE.search(url).group(1) == '17339_224333'


class TestAnnouncementFilter:
    def test_candidate_score(self):
        assert _candidate_score('住房城乡建设部关于发布国家标准《建筑设计防火规范》的公告') > 0
        assert _candidate_score('……局部修订条文的公告') < 0
        assert _candidate_score('……英文版的公告') <= -100
        assert _candidate_score('关于实施……有关要求的通知') <= -100

    def test_extract_name(self):
        assert _extract_name('住房城乡建设部关于发布国家标准《建筑设计防火规范》的公告') == '建筑设计防火规范'
        assert _extract_name('无书名号的标题') == ''


def _ann(i):
    return {'id': f'a{i}', 'title': f'关于发布国家标准《测试标准{i}》的公告',
            'url': f'https://www.mohurd.gov.cn/gongkai/zc/wjk/art/2025/art_a{i}.html'}


def test_scan_builds_items_and_advances_checkpoint(monkeypatch):
    saved = {}
    monkeypatch.setattr(mohurd_scan, 'get_delay', lambda: 0)
    monkeypatch.setattr(mohurd_scan, 'get_incr_checkpoint', lambda *a: None)
    monkeypatch.setattr(mohurd_scan, 'update_incr_checkpoint',
                        lambda t, k, data: saved.update(data))
    monkeypatch.setattr(mohurd_scan, '_fetch_announcement_list',
                        lambda page, size=100: [_ann(1), _ann(2)] if page == 1 else [])

    def fake_detail(url, title):
        return {'codes': ['GB 50001-2025'], 'attachments': [
            {'url': 'https://example.com/a.pdf', 'label': '附件'}],
            'publishDate': '2025-01-01', 'name': '测试标准1'}
    monkeypatch.setattr(mohurd_scan, '_fetch_announcement', fake_detail)

    items = asyncio.run(mohurd_scan.scan_mohurd_standards(
        max_results=1, keyword_group='__all__', status='全部'))
    assert len(items) == 1
    assert items[0]['code'] == 'GB 50001-2025'
    assert items[0]['pdf_url'] == 'https://example.com/a.pdf'
    assert saved['first_id'] == 'a1'
    assert saved['filter'] == 'mohurd'


def test_incremental_hits_previous_announcement(monkeypatch):
    monkeypatch.setattr(mohurd_scan, 'get_delay', lambda: 0)
    monkeypatch.setattr(mohurd_scan, 'get_incr_checkpoint',
                        lambda *a: {'first_id': 'a1', 'filter': 'mohurd'})
    monkeypatch.setattr(mohurd_scan, 'update_incr_checkpoint',
                        lambda *a: (_ for _ in ()).throw(AssertionError('不应更新')))
    monkeypatch.setattr(mohurd_scan, '_fetch_announcement_list',
                        lambda page, size=100: [_ann(1), _ann(2)] if page == 1 else [])

    items = asyncio.run(mohurd_scan.scan_mohurd_standards(
        max_results=10, incr=True, keyword_group='__all__', status='全部'))
    assert items == []


def test_skips_non_standard_announcements(monkeypatch):
    monkeypatch.setattr(mohurd_scan, 'get_delay', lambda: 0)
    monkeypatch.setattr(mohurd_scan, 'get_incr_checkpoint', lambda *a: None)
    monkeypatch.setattr(mohurd_scan, 'update_incr_checkpoint', lambda *a: None)
    monkeypatch.setattr(mohurd_scan, '_fetch_announcement_list',
                        lambda page, size=100: [
                            {'id': 'x', 'title': '关于征求国家标准意见的通知', 'url': 'u'},
                            _ann(2)] if page == 1 else [])
    monkeypatch.setattr(mohurd_scan, '_fetch_announcement',
                        lambda url, title: {'codes': ['GB 50002-2025'],
                                            'attachments': [], 'publishDate': '',
                                            'name': '测试标准2'})

    items = asyncio.run(mohurd_scan.scan_mohurd_standards(
        max_results=10, keyword_group='__all__', status='全部'))
    assert [it['code'] for it in items] == ['GB 50002-2025']
