"""扫描分页骨架测试：增量截止、max_results、筛选作用域"""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.scanner import scan_core


def _items(start, count):
    return [{'detail_id': str(i), 'code': f'GB {i}', 'name': f'标准{i}'}
            for i in range(start, start + count)]


def _run(monkeypatch, pages, **kwargs):
    saved = {}
    ckpt = kwargs.pop('_ckpt', None)
    monkeypatch.setattr(scan_core, 'get_delay', lambda: 0)
    monkeypatch.setattr(scan_core, 'get_incr_checkpoint', lambda *a: ckpt)
    monkeypatch.setattr(scan_core, 'update_incr_checkpoint',
                        lambda t, k, data: saved.update(data))

    async def fetch(page):
        return pages[page - 1] if page <= len(pages) else []

    result = asyncio.run(scan_core.scan_pages(
        fetch_page=fetch,
        scan_type='test',
        page_size=kwargs.pop('page_size', 5),
        **kwargs,
    ))
    return result, saved


def test_pagination_respects_max_results(monkeypatch):
    pages = [_items(1, 5), _items(6, 5), _items(11, 5)]
    result, saved = _run(monkeypatch, pages, max_results=7)
    assert [it['detail_id'] for it in result] == [str(i) for i in range(1, 8)]
    assert saved['first_id'] == '1'
    assert saved['count'] == 7


def test_incremental_scans_past_max_results(monkeypatch):
    pages = [_items(i, 5) for i in range(1, 21, 5)]
    result, saved = _run(
        monkeypatch, pages, max_results=5, incr=True,
        _ckpt={'first_id': '12', 'filter': ''})
    assert len(result) == 11
    assert result[-1]['detail_id'] == '11'
    assert saved['first_id'] == '1'


def test_hard_cap_does_not_advance_checkpoint(monkeypatch):
    pages = [_items(i, 5) for i in range(1, 501, 5)]
    result, saved = _run(
        monkeypatch, pages, max_results=5, incr=True,
        _ckpt={'first_id': 'missing', 'filter': ''})
    assert len(result) == 150
    assert saved == {}


def test_filter_change_ignores_checkpoint(monkeypatch):
    pages = [_items(1, 5)]
    result, saved = _run(
        monkeypatch, pages, max_results=5, incr=True, filter_key='现行|安全',
        _ckpt={'first_id': '3', 'filter': '废止|安全'})
    assert len(result) == 5
    assert saved['filter'] == '现行|安全'


def test_gb_incremental_scans_past_max_results(monkeypatch):
    from app.scanner import gb_scan
    rows = [{'id': str(i), 'C_STD_CODE': f'GB {i}', 'C_C_NAME': f'测试{i}'}
            for i in range(1, 151)]

    def fake_fetch(page, state):
        return {'rows': rows[(page - 1) * 50:page * 50]}

    saved = {}
    monkeypatch.setattr(gb_scan, 'fetch_api_page', fake_fetch)
    monkeypatch.setattr(gb_scan, 'get_delay', lambda: 0)
    monkeypatch.setattr(gb_scan, 'is_safety', lambda *a, **k: True)
    monkeypatch.setattr(gb_scan, 'is_aq_yj', lambda *a: False)
    monkeypatch.setattr(gb_scan, 'get_incr_checkpoint',
                        lambda *a: {'first_id': '120'})
    monkeypatch.setattr(gb_scan, 'update_incr_checkpoint',
                        lambda t, k, data: saved.update(data))

    result = asyncio.run(gb_scan.scan_pages(
        max_results=50, incr=True, keyword_group='__all__'))
    assert len(result) == 119
    assert result[-1]['id'] == '119'
    assert saved['first_id'] == '1'


def test_hb_incremental_scans_past_max_results(monkeypatch):
    from app.scanner import hb_scan
    records = [{'pk': str(i), 'chName': f'测试{i}', 'code': f'GB {i}',
                'status': '现行', 'industry': 'A', 'issueDate': '', 'actDate': ''}
               for i in range(1, 201)]

    def fake_fetch(item, page, size=100):
        return {'records': records[(page - 1) * 100:page * 100],
                'total': 200, 'pages': 2}

    saved = {}
    monkeypatch.setattr(hb_scan, 'get_delay', lambda: 0)
    monkeypatch.setattr(hb_scan, 'is_safety', lambda *a, **k: True)
    monkeypatch.setattr(hb_scan, 'is_aq_yj', lambda *a: False)
    monkeypatch.setattr(hb_scan, 'get_incr_checkpoint',
                        lambda *a: {'first_pk': '120'})
    monkeypatch.setattr(hb_scan, 'update_incr_checkpoint',
                        lambda t, k, data: saved.update(data))

    result = hb_scan._scan_list_standards(
        ['A'], '行业', fake_fetch, 'hb', 'TEST',
        max_results=50, incr=True, filter_key='x')
    assert len(result) == 119
    assert saved['first_pk'] == '1'


def test_post_process_filters_before_checkpoint(monkeypatch):
    pages = [[
        {'detail_id': '1', 'status': '现行'},
        {'detail_id': '2', 'status': '废止'},
    ]]
    result, saved = _run(
        monkeypatch, pages, max_results=5, post_process=lambda items: [
            it for it in items if it['status'] == '现行'])
    assert [it['detail_id'] for it in result] == ['1']
    assert saved['first_id'] == '1'
