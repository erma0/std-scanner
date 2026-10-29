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

# 标准编号前缀（含 DB11/T 这类带数字的分部分前缀），用于在缺少 en-code 标签
# 或名称误带编号时定位名称的起点
_CODE_PREFIX_RE = re.compile(
    r'^\s*([A-Z]{1,6}\d*\s*(?:/\s*[A-Z]{1,4})?\s*\d+(?:\.\d+)?\s*[-—–]\s*\d{2,4})'
)


def _collapse_text(text):
    """压缩连续空白为单个空格并去除首尾空白"""
    return re.sub(r'\s+', ' ', text or '').strip()


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

        # 搜索结果会把命中的关键词包进 <sacinfo> 标签，编号会被拆成多个文本节点
        # （如 <sacinfo>JB</sacinfo>/<sacinfo>T</sacinfo> <sacinfo>3340</sacinfo>-2025）。
        # 用 string(.) 取完整文本，避免拼接文本节点时凭空插入空格；
        # 直接取 span.en-code::text 只能拿到第一个文本节点（常为 "/"）。
        full_text = _collapse_text(link.xpath('string(.)').get(''))
        en_code = _collapse_text(link.css('span.en-code').xpath('string(.)').get(''))

        # 名称前缀即标准编号，用正则匹配出编号起点，保证名称不含编号
        prefix_match = _CODE_PREFIX_RE.match(full_text)
        raw_code = en_code or (prefix_match.group(1) if prefix_match else '')
        if prefix_match:
            name = full_text[prefix_match.end():].strip()
        elif en_code and full_text.startswith(en_code):
            name = full_text[len(en_code):].strip()
        else:
            name = full_text

        code = normalize_code(raw_code)

        # 状态标签有 label-success/label-danger 等多种样式，第一个 s-status 才是
        # 标准状态（post-flex 里还有一个空的 s-status，需取首个非空文本）
        status = next(
            (t.strip() for t in panel.css('span.s-status::text').getall() if t.strip()),
            '').strip()

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
    elif tid == 'BV_TT':
        # 团体标准详情页（ttbz.org.cn）
        return f"https://www.ttbz.org.cn/StandardManage/Detail/{pid}/", tid
    else:
        return f"https://std.samr.gov.cn/gb/search/gbDetailed?id={pid}", tid


def check_downloadable(tid):
    """检查标准类型是否可下载"""
    if tid == 'BV_GB':
        return True, '国家标准，可下载'
    elif tid == 'BV_HB':
        return True, '行业标准，可下载'
    elif tid == 'BV_DB':
        return True, '地方标准，可下载'
    elif tid == 'BV_TT':
        return True, '团体标准，可下载'
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
