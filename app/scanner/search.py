"""scanner.search — 标准检索网站搜索"""

import re
import logging

from config.settings import SEARCH_PAGE, http_client
from app.helpers import normalize_code

try:
    from parsel import Selector
except ImportError:
    Selector = None

_log = logging.getLogger('std_scraper')


def fetch_stdpage_search(query, page=1, std_type='国家标准'):
    """使用标准检索网站搜索标准（HTML 页面）"""
    url = SEARCH_PAGE
    params = {
        'q': query,
        'tid': '',
        'pageNo': page,
    }

    if std_type and std_type != '全部':
        params['op'] = f'G_STD_DOMAIN:"{std_type}"'

    try:
        resp = http_client.get(url, params=params)
        resp.raise_for_status()
    except Exception as e:
        _log.error(f"[ERROR] 搜索请求失败: {e}")
        return [], 0

    sel = Selector(resp.text)
    standards = []

    total_text = sel.css('.nums span::text').get('')
    try:
        total_count = int(total_text.replace(',', ''))
    except (ValueError, AttributeError):
        total_count = 0

    panels = sel.css('div.panel-default')

    for panel in panels:
        link = panel.css('a[pid]')
        if not link:
            continue

        tid = link.attrib.get('tid', '')
        pid = link.attrib.get('pid', '')

        all_text_parts = link.css('::text').getall()
        full_text = ''.join(t.strip() for t in all_text_parts if t.strip())

        code = link.css('span.en-code::text').get('').strip()
        if not code:
            code = full_text.split()[0] if full_text else ''

        code = normalize_code(code)

        name = full_text.replace(code, '').strip()

        status = panel.css('span.s-status.label-info::text').get('').strip()

        ics = panel.css('span:contains("ICS") + span::text').get('').strip()
        ccs = panel.css('span:contains("CCS") + span::text').get('').strip()

        time_texts = panel.css('time.post-date::text').getall()
        publish_date = time_texts[0] if len(time_texts) > 0 else ''
        act_date = time_texts[1] if len(time_texts) > 1 else ''

        standards.append({
            'code': code,
            'name': name,
            'tid': tid,
            'pid': pid,
            'status': status,
            'ics': ics,
            'ccs': ccs,
            'publishDate': publish_date,
            'actDate': act_date,
            'searchType': std_type,
        })

    return standards, total_count


def get_detail_url_by_tid(tid, pid):
    """根据 tid 和 pid 获取详情页 URL"""
    if tid == 'BV_HB':
        return f"https://std.samr.gov.cn/hb/search/stdHBDetailed?id={pid}", tid
    elif tid == 'BV_DB':
        return f"https://std.samr.gov.cn/db/search/stdDBDetailed?id={pid}", tid
    elif tid == 'BV_GB_PLAN':
        return f"https://std.samr.gov.cn/gb/search/stdPlanDetailed?id={pid}", tid
    else:
        return f"https://std.samr.gov.cn/gb/search/gbDetailed?id={pid}", tid


def check_downloadable(tid):
    """检查标准类型是否可下载"""
    if tid == 'BV_GB':
        return True, '国家标准，可下载'
    elif tid == 'BV_HB':
        return True, '行业标准，可下载（通过验证码）'
    elif tid == 'BV_DB':
        return True, '地方标准，可下载（通过验证码）'
    elif tid == 'BV_GB_PLAN':
        return False, '国家标准计划，仅计划信息，无PDF'
    else:
        return False, f'未知类型({tid})'


def check_hb_downloadable(detail_url):
    """从行业标准/地方标准详情页检查是否可下载并提取附件hash"""
    resp = http_client.get(detail_url)
    resp.raise_for_status()

    patterns = [
        r'hbba\.sacinfo\.org\.cn/attachment/onlineRead/([a-f0-9]{64})',
        r'dbba\.sacinfo\.org\.cn/portal/online/([a-f0-9]{64})',
    ]

    for pattern in patterns:
        match = re.search(pattern, resp.text, re.I)
        if match:
            return True, match.group(1), pattern

    return False, None, None
