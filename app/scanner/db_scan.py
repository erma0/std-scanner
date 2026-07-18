"""scanner.db_scan — 地方标准扫描与下载"""

import logging
from urllib.parse import urlencode

from config.settings import DB_API_URL, http_client
from app.keywords import set_active_group
from app.scanner.hb_scan import _scan_list_standards, _download_standards

_log = logging.getLogger('std_scraper')


def fetch_db_list(province='', key='', status=None, page=1, size=100):
    """调用地方标准列表 API

    DB status 为单选（不同于 HB 的多选），参数格式为 status=现行 而非 status[]=现行
    DB 网站支持的状态: 现行/废止/''(全部)。
    '即将实施' 在 DB 网站不支持（DB 新标准发布即生效，无预告期），回退到 '现行'。
    """
    # DB 网站不支持 '即将实施'，回退到 '现行'
    if status == '即将实施':
        status = '现行'
    params = {
        'current': page, 'size': size, 'key': key,
        'ministry': '', 'industry': province,
        'pubdate': '', 'date': '',
    }
    if status:
        if isinstance(status, str) and ',' in status:
            status = status.split(',')[0]
        params['status'] = status
    else:
        params['status'] = '现行'
    encoded = urlencode(params)
    resp = http_client.post(DB_API_URL, content=encoded.encode(), headers={
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Referer': 'https://dbba.sacinfo.org.cn/stdList',
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-Requested-With': 'XMLHttpRequest',
        'Origin': 'https://dbba.sacinfo.org.cn',
    })
    resp.raise_for_status()
    return resp.json()


def scan_db_standards(provinces=None, max_results=500, incr=False, keyword_group=None, on_progress=None, on_intermediate=None, check_pause=None, status='现行'):
    """地方标准扫描（支持 max_results + 增量）

    status: 标准状态筛选 — ''(全部), '现行', '即将实施', '废止'。默认 '现行'

    on_progress: 可选 callable(pct, msg)，每扫描一页调用一次（同步）
    on_intermediate: 可选 callable(standards)，每扫描一页后调用（同步），用于推送中间结果
    check_pause: 可选 callable() → bool，每页前调用（同步）
    """
    set_active_group(keyword_group or '安全生产')

    if provinces is None:
        provinces = ['江苏省']
    elif isinstance(provinces, str):
        provinces = [provinces]
    # 空列表 → 留空模式：API 不限制省份，返回全部混合数据
    if not provinces:
        provinces = ['']

    def _fetch(item, page, size):
        return fetch_db_list(province=item, page=page, size=size, status=status)

    return _scan_list_standards(provinces, '省份', _fetch, 'db', 'DB-SCAN', max_results, incr, on_progress=on_progress, on_intermediate=on_intermediate, check_pause=check_pause)


async def download_db_standards(standards, on_progress=None, on_item_done=None, check_pause=None):
    """地方标准下载

    on_progress: 可选 async callable(pct, msg)
    on_item_done: 可选 async callable(item_name='')，每完成一条标准处理后调用
    check_pause: 可选 async callable() → bool
    """
    await _download_standards(standards, 'DB-DL', on_progress=on_progress, on_item_done=on_item_done, check_pause=check_pause)
