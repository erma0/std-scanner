"""scanner.search — 标准检索网站搜索"""

import re
import time
import logging

import httpx

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


# 查询/结果编号解析：兼容 "GB/T 50016-2014"、"DB11/T 123-2020"、"T/CECS 1-2020"
# 以及纯数字 "50016"；末尾允许“（2018年版）”这类后缀
_STD_CODE_RE = re.compile(
    r'^\s*([A-Za-z]{1,6}\d*(?:\s*/\s*[A-Za-z]{1,4})?)\s*(\d+(?:\.\d+)?)'
    r'\s*(?:[-—–]\s*(\d{2,4}))?\s*(?:[（(].*)?$'
)
# 紧凑编号（GB50016-2014 / GB/T50145-2007）：前缀不含数字，与 DB11/T 这类
# 带地区码的前缀区分，避免把 50016 的前几位吞进 prefix
_COMPACT_CODE_RE = re.compile(
    r'^\s*([A-Za-z]{1,6}(?:\s*/\s*[A-Za-z]{1,4})?)\s*(\d+(?:\.\d+)?)'
    r'\s*(?:[-—–]\s*(\d{2,4}))?\s*(?:[（(].*)?$'
)
_STD_NUMBER_RE = re.compile(
    r'^\s*(\d+(?:\.\d+)?)\s*(?:[-—–]\s*(\d{2,4}))?\s*(?:[（(].*)?$'
)


def parse_standard_code(text):
    """宽松解析标准编号，返回 {'prefix', 'part', 'year'}；非编号返回 None。"""
    if not text:
        return None
    norm = normalize_code(text.strip())
    for pattern in (_COMPACT_CODE_RE, _STD_CODE_RE):
        m = pattern.match(norm)
        if m:
            prefix = re.sub(r'\s+', '', m.group(1) or '').upper()
            return {'prefix': prefix, 'part': m.group(2),
                    'year': m.group(3) or ''}
    m = _STD_NUMBER_RE.match(norm)
    if m:
        return {'prefix': '', 'part': m.group(1), 'year': m.group(2) or ''}
    return None


# 这些体系的编号不使用 /T 表示推荐性，套用会得到无意义的编号
_NO_T_PREFIXES = {
    'JJF', 'JJG', 'TSG', 'CECS', 'GB/Z',
    'ISO', 'IEC', 'EN', 'ANSI', 'BS', 'DIN', 'JIS', 'NF', 'AS', 'ASTM',
}


def alternate_code_prefix(code):
    """编号纠错：在 推荐性(/T) 与 强制性 前缀之间切换。

    例：GB → GB/T、HB → HB/T、DB11/T → DB11、JGJ/T → JGJ。
    团体标准（T/...）及 JJF/JJG/TSG 等没有 /T 体系的编号不处理。
    """
    info = parse_standard_code(code)
    if not info:
        return ''
    prefix = info['prefix']
    if not prefix or prefix.startswith('T/') or prefix in _NO_T_PREFIXES:
        return ''
    if prefix.endswith('/T'):
        alt = prefix[:-2]
    else:
        alt = f'{prefix}/T'
    suffix = f"-{info['year']}" if info.get('year') else ''
    return f"{alt} {info['part']}{suffix}"


# GBZ/GBZ/T 是国家职业卫生标准，与 GB/Z（国家标准化指导性技术文件）分属
# 两个编号体系。国标检索库不收录 GBZ，且检索时会把两者混在一起返回同号结果，
# 容易被误判成"旧版已被新版替代"
_SITE_UNCOVERED_PREFIXES = {'GBZ', 'GBZ/T'}


def site_uncovered_note(query):
    """编号体系不在检索站点收录范围时返回说明，否则空字符串。"""
    info = parse_standard_code(query)
    if not info or info['prefix'] not in _SITE_UNCOVERED_PREFIXES:
        return ''
    return (f'{info["prefix"]} 是国家职业卫生标准，与 GB/Z（指导性技术文件）'
            f'不是同一体系；国标检索库未收录该系列，返回的 GB/Z 为同号无关标准，'
            f'需到卫健委标准网查询')


def other_year_versions(query, standards, limit=2):
    """同前缀同编号、仅年份不同的结果描述（用于提示站点的其他版本）。

    仅在查询指定了年份时返回，避免把"未指定年份"的正常匹配误报成版本差异。
    """
    info = parse_standard_code(query)
    if not info or not info['prefix'] or not info['year']:
        return []
    out = []
    for s in standards:
        si = parse_standard_code(s.get('code', ''))
        if not si or si['prefix'] != info['prefix'] or si['year'] == info['year']:
            continue
        if si['part'] != info['part'] and not si['part'].startswith(info['part'] + '.'):
            continue
        label = f"{s.get('code')}（{s.get('status') or '状态未知'}）"
        if label not in out:
            out.append(label)
    return out[:limit]


def filter_standard_results(query, standards, max_results=None):
    """按搜索词意图过滤结果，返回 (items, is_code_query)。

    - 关键词：保持站点顺序，最多 max_results 条；
    - 标准号：只保留编号匹配的结果（指定年份则精确到年份）；未指定年份且
      存在现行/即将实施版本时，自动排除已废止版本，避免批量下载历史版本。
    """
    code_info = parse_standard_code(query)
    if not code_info:
        return (standards[:max_results] if max_results else list(standards)), False

    def _match(s):
        info = parse_standard_code(s.get('code', ''))
        if not info:
            return False
        # 允许基础号命中其分部分（如 GB/T 7588 → GB/T 7588.1-2020）
        part = code_info['part']
        if info['part'] != part and not info['part'].startswith(part + '.'):
            return False
        if code_info['year'] and info['year'] != code_info['year']:
            return False
        if code_info['prefix'] and info['prefix'] != code_info['prefix']:
            return False
        return True

    matched = [s for s in standards if _match(s)]
    if not code_info['year'] and matched:
        current = [s for s in matched if s.get('status') in ('现行', '即将实施')]
        if current:
            matched = current
    return matched, True


# 站点偶发 5xx/429（与检索词无关），有限重试可避免把服务端抖动当成"查询失败"
_SEARCH_RETRIES = 2
_SEARCH_RETRY_STATUS = {429, 500, 502, 503, 504}


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

    resp, last_err = None, None
    for attempt in range(_SEARCH_RETRIES + 1):
        try:
            resp = http_client.get(url, params=params, timeout=30.0)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_err = e
        else:
            if resp.status_code not in _SEARCH_RETRY_STATUS:
                break
            last_err = httpx.HTTPStatusError(
                f'站点返回 {resp.status_code}', request=resp.request, response=resp)
        if attempt < _SEARCH_RETRIES:
            wait = 2 * (attempt + 1)
            _log.warning(f"[SEARCH] 搜索请求失败（{type(last_err).__name__}: {last_err}），"
                         f"{wait}s 后重试: query={query!r}")
            time.sleep(wait)

    if resp is None or resp.status_code in _SEARCH_RETRY_STATUS:
        # 不吞异常：调用方需要区分"站点无结果"和"请求失败"，否则日志会误导用户
        raise RuntimeError(f'搜索请求失败: {type(last_err).__name__}: {last_err}') from last_err
    try:
        resp.raise_for_status()
    except Exception as e:
        raise RuntimeError(f'搜索请求失败: {type(e).__name__}: {e}') from e

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

    # 页面显示有结果但没有解析出条目：多半是页面改版或被风控拦截，必须留下明确线索
    if total_count and not standards:
        _log.warning(f"[SEARCH] 页面显示共 {total_count} 条但未解析出结果: "
                     f"query={query!r}, type={std_type}")

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
    """检查标准类型是否支持下载（仅按类型判断，不代表该标准一定能取到全文）"""
    if tid == 'BV_GB':
        return True, '国家标准，支持下载'
    elif tid == 'BV_HB':
        return True, '行业标准，支持下载'
    elif tid == 'BV_DB':
        return True, '地方标准，支持下载'
    elif tid == 'BV_TT':
        return True, '团体标准，支持下载'
    elif tid == 'BV_MOHURD':
        return True, '住建部公告附件，支持下载'
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
