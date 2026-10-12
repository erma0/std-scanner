"""scanner.samr_scan — 市场监督管理总局政策法规扫描与下载

市场监管总局 - 特种设备监察局 政策法规页
入口：https://www.samr.gov.cn/tzsbj/zcfg/index.html

仅扫描有更新的两个栏目（规范性文件/地方法规长期无更新，不扫描）：

1) 法律法规 (flfg)：https://www.samr.gov.cn/tzsbj/zcfg/flfg/index.html
   - 列表通过 jpaas-publish-server API 动态渲染
   - 条目为部门规章/法律（无标准号），标题含"第N号令"等
   - 详情页提供 PDF + DOCX 下载（gzdownloadcon 区域）
   - 状态：标题含"废止" → '废止'，否则 '现行'

2) 安全技术规范 (aqjsgf)：https://www.samr.gov.cn/tzsbj/zcfg/aqjsgf/index.html
   - 同为 API 动态渲染列表
   - 条目为 TSG 系列安全技术规程（如 TSG 51—2023）
   - 详情页附件下载区（contentLeft0102box）提供 PDF
   - 部分条目列表 href 直接指向 PDF（无需访问详情页）
   - 状态：标题含"废止" → '废止'，否则 '现行'
   - 注意：该栏目是历年全量归档（2005 年以来的旧版本也在列，且网站不标注废止），
     扫描结束后统一由 _mark_superseded_status 按"替代清单 + 同号取新"把旧版标为 '废止'

API 调用：
  GET /api-gateway/jpaas-publish-server/front/page/build/unit
  参数：parseType=bulidstatic, webId, tplSetId, pageType=column,
        tagId=ajax分页, editType=null, pageId(栏目ID),
        paramJson={"pageNo": N, "pageSize": 10}
  返回：JSON {data: {html: "列表HTML（含分页信息）"}}
  分页：从 HTML 中 <div class="pagination" count="总数" rows="页大小"> 提取总条数

文章 ID：
  - 详情页 URL：/tzsbj/zcfg/aqjsgf/art/2026/art_XXXX.html → XXXX（32位hex）
  - 直接 PDF URL：取文件名（如 P020230407313550673854）作为 ID

字段映射（与 MEM 保持一致）：
  type='mem', source='flfg'/'aqjsgf', tid='BV_SAMR_FLFG'/'BV_SAMR_AQJSGF'
  code：aqjsgf 提取 TSG 编号；flfg 留空
  name：标题（剥离 TSG 编号括号与书名号《》）

增量扫描：用文章 ID（或 PDF 文件名）作为比对字段。
"""

import asyncio
import json
import logging
import re
import urllib.parse
from typing import Callable

from app.helpers import normalize_code, _DASH_NORMALIZE_TABLE
from app.keywords import is_safety, set_active_group
from app.scanner.checkpoint import get_incr_checkpoint, update_incr_checkpoint
from app.scanner.utils import make_filename
from config.settings import (
    SAMR_BASE, SAMR_API_URL, SAMR_WEB_ID, SAMR_TPL_SET_ID,
    SAMR_PAGE_IDS, SAMR_PAGE_SIZE,
    get_output_dir, get_delay, http_client,
)

_log = logging.getLogger('std_scraper')

# ==================== 常量 ====================
_SAMR_DL_STATUS = 'dlStatus'  # 与 GB/HB/DB/TT/MEM 一致
_SAMR_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

# 源 → 栏目 pageId
_SOURCE_PAGE_ID = SAMR_PAGE_IDS  # {'flfg': ..., 'aqjsgf': ...}

# 源 → tid
_SOURCE_TID = {
    'flfg': 'BV_SAMR_FLFG',
    'aqjsgf': 'BV_SAMR_AQJSGF',
}

# 源 → 中文标签
_SOURCE_LABEL = {
    'flfg': '法律法规',
    'aqjsgf': '安全技术规范',
}


def _samr_headers(source: str = 'flfg') -> dict:
    """构造 samr.gov.cn 请求头。"""
    col = 'flfg' if source == 'flfg' else 'aqjsgf'
    return {
        'User-Agent': _SAMR_UA,
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Referer': f'{SAMR_BASE}/tzsbj/zcfg/{col}/index.html',
    }


# ==================== API 调用 ====================
def _call_list_api(page_no: int, source: str = 'aqjsgf') -> tuple:
    """调用 jpaas-publish-server API 获取列表页 HTML。

    Returns:
        (html, total_count): 列表 HTML 片段 + 总条数
    """
    page_id = _SOURCE_PAGE_ID.get(source)
    if not page_id:
        raise ValueError(f"未知 SAMR 源: {source}")

    params = {
        'parseType': 'bulidstatic',
        'webId': SAMR_WEB_ID,
        'tplSetId': SAMR_TPL_SET_ID,
        'pageType': 'column',
        'tagId': 'ajax分页',
        'editType': 'null',
        'pageId': page_id,
        'paramJson': json.dumps({'pageNo': page_no, 'pageSize': SAMR_PAGE_SIZE}),
    }
    resp = http_client.get(SAMR_API_URL, params=params, headers=_samr_headers(source), timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not data.get('success'):
        raise RuntimeError(f"SAMR API 返回失败: {data.get('message', '未知错误')}")
    html = data.get('data', {}).get('html', '')

    # 从分页 div 提取总条数：<div class="pagination" count="111" rows="10">
    total = 0
    m = re.search(r'count="(\d+)"', html)
    if m:
        total = int(m.group(1))
    return html, total


# ==================== 列表解析 ====================
# 列表项：<li class="content-3-left-text ..."><a href="..." title="标题">标题</a><div class="contentRight01time">日期</div></li>
_LIST_ITEM_RE = re.compile(
    r'<li\s+class="content-3-left-text[^"]*">\s*'
    r'<a\s+href="([^"]+)"\s+title="([^"]*)"[^>]*>.*?</a>\s*'
    r'<div\s+class="contentRight01time">([^<]*)</div>',
    re.DOTALL,
)

# 文章 ID：art/2026/art_d04733c1b8fb4011b86424e4560e67ff.html → d04733c1b8fb4011b86424e4560e67ff
_ARTICLE_ID_RE = re.compile(r'art_([a-f0-9]+)\.html')

# 直接 PDF 文件名：P020230407313550673854.pdf → P020230407313550673854
_PDF_FILENAME_RE = re.compile(r'/([^/]+)\.pdf$', re.IGNORECASE)

# TSG 编号：TSG [字母前缀]{0,2} 数字 - 年份（前缀含 G/Q/R/S/N/T/D/Z/ZB/ZF/RF 等）
# 先做 dash 归一化再匹配
_TSG_CODE_RE = re.compile(r'TSG\s*([A-Z]{1,2})?\s*(\d{1,4})\s*-\s*(\d{4})')
# 标题中包裹编号的括号（全角/半角），如 '（TSG G7002—2015）'
_TSG_BRACKET_RE = re.compile(r'[（(]\s*TSG\s*[A-Z]{0,2}\s*\d{1,4}\s*-\s*\d{4}\s*[）)]')


def _parse_tsg_title(title: str) -> tuple:
    """解析含 TSG 编号的标题，返回 (code, name)。

    示例：
      '特种设备焊接操作人员考核细则（TSG Z6002—2026）'
        → ('TSG Z6002-2026', '特种设备焊接操作人员考核细则')
      '锅炉定期检验规则（TSG G7002-2015）'
        → ('TSG G7002-2015', '锅炉定期检验规则')
      '《起重机械安全技术规程》（TSG 51—2023）'
        → ('TSG 51-2023', '起重机械安全技术规程')
      '《特种设备检验机构核准规则》（TSG Z7001—2021）第 1 号修改单'
        → ('TSG Z7001-2021', '特种设备检验机构核准规则 第1号修改单')
      '电梯自行检测规则(TSG T7008-2023)'
        → ('TSG T7008-2023', '电梯自行检测规则')
      '《起重机械安全技术规程（第1号修改单）》'  (无 TSG 编号)
        → ('', '起重机械安全技术规程（第1号修改单）')
    """
    # 归一化 dash 字符
    normalized = title.translate(_DASH_NORMALIZE_TABLE)
    m = _TSG_CODE_RE.search(normalized)
    if not m:
        # 无 TSG 编号：剥离《》后整段作为 name
        name = normalized.strip()
        if name.startswith('《'):
            name = name[1:]
        if name.endswith('》'):
            name = name[:-1]
        return '', name.strip()

    letter, num, year = m.group(1) or '', m.group(2), m.group(3)
    code = f'TSG {letter}{num}-{year}' if letter else f'TSG {num}-{year}'
    code = normalize_code(code)

    # 从标题中移除 TSG 编号及其包裹的括号
    name = _TSG_BRACKET_RE.sub('', normalized).strip()
    # 剥离《》
    if name.startswith('《'):
        name = name[1:]
    if name.endswith('》'):
        name = name[:-1]
    name = name.strip()
    return code, name


def _parse_status(title: str) -> str:
    """根据标题判断状态。"""
    if '废止' in title:
        return '废止'
    return '现行'


# ==================== 旧版/失效识别 ====================
# 归档栏目本身不标注废止状态，以下替代关系来自各新规程前言"整合修订"清单与总局发布公告，
# 已核实来源（节选）：
#   TSG 11—2020 前言：整合 G0001/G1001/ZB001/ZB002/G5001/G5002/G5003/G7001/G7002 共 9 个规范
#   TSG 91—2021 前言：整合 G0002/G0003
#   TSG 21—2016 前言：整合 R7001/R7004 等 7 个规范
#   TSG 23—2021 前言：整合 R0006/R0009/RF001/R1003/R7002/R7003 等 7 个规范
#   TSG 51—2023 前言：整合 Q0002/Q7015/Q7016 等（Q 系列型式试验细则经 Q7002—2019 一并取代）
#   TSG T7001—2023 前言：整合 TSG T7001～T7006
#   TSG 81—2022：N0001—2017 的修订版
#   TSG 31—2025 公告：整合 D0001/D7005 等
#   TSG 92—2026 公告：整合 ZF001/ZF003 等
#   TSG Z7001—2021：整合 Z7001/Z7002/Z7003—2004；TSG Z7002—2022：整合 Z7005—2015
#   TSG 08—2026（2026-05-01 施行，总局公告）：取代 08—2017
_TSG_SUPERSEDED = {
    # 锅炉（TSG 11—2020）
    'TSG G0001-2012': 'TSG 11-2020',
    'TSG G1001-2004': 'TSG 11-2020',
    'TSG ZB001-2008': 'TSG 11-2020',
    'TSG ZB002-2008': 'TSG 11-2020',
    'TSG G5001-2010': 'TSG 11-2020',
    'TSG G5002-2010': 'TSG 11-2020',
    'TSG G5003-2008': 'TSG 11-2020',
    'TSG G7001-2015': 'TSG 11-2020',
    'TSG G7002-2015': 'TSG 11-2020',
    # 锅炉节能（TSG 91—2021）
    'TSG G0002-2010': 'TSG 91-2021',
    'TSG G0003-2010': 'TSG 91-2021',
    # 固定式压力容器（TSG 21—2016）
    'TSG R7001-2013': 'TSG 21-2016',
    'TSG R7004-2013': 'TSG 21-2016',
    # 气瓶（TSG 23—2021）
    'TSG R0006-2014': 'TSG 23-2021',
    'TSG R0009-2009': 'TSG 23-2021',
    'TSG R1003-2006': 'TSG 23-2021',
    'TSG RF001-2009': 'TSG 23-2021',
    'TSG R7002-2009': 'TSG 23-2021',
    'TSG R7003-2011': 'TSG 23-2021',
    # 起重机械（TSG 51—2023，含经 Q7002—2019 取代的型式试验细则系列）
    'TSG Q0002-2008': 'TSG 51-2023',
    'TSG Q7015-2016': 'TSG 51-2023',
    'TSG Q7016-2016': 'TSG 51-2023',
    'TSG Q7002-2007': 'TSG 51-2023',
    'TSG Q7003-2007': 'TSG 51-2023',
    'TSG Q7004-2006': 'TSG 51-2023',
    'TSG Q7005-2008': 'TSG 51-2023',
    'TSG Q7006-2007': 'TSG 51-2023',
    'TSG Q7007-2007': 'TSG 51-2023',
    'TSG Q7008-2007': 'TSG 51-2023',
    'TSG Q7009-2007': 'TSG 51-2023',
    'TSG Q7010-2007': 'TSG 51-2023',
    'TSG Q7012-2008': 'TSG 51-2023',
    'TSG Q7013-2006': 'TSG 51-2023',
    'TSG Q7014-2008': 'TSG 51-2023',
    # 电梯检验（TSG T7001—2023 整合 T7001～T7006）
    'TSG T7001-2009': 'TSG T7001-2023',
    'TSG T7002-2011': 'TSG T7001-2023',
    'TSG T7003-2011': 'TSG T7001-2023',
    'TSG T7004-2012': 'TSG T7001-2023',
    'TSG T7005-2012': 'TSG T7001-2023',
    'TSG T7006-2012': 'TSG T7001-2023',
    # 场车（TSG 81—2022）
    'TSG N0001-2017': 'TSG 81-2022',
    # 工业管道（TSG 31—2025）
    'TSG D0001-2009': 'TSG 31-2025',
    'TSG D7005-2018': 'TSG 31-2025',
    # 承压类安全附件（TSG 92—2026）
    'TSG ZF001-2006': 'TSG 92-2026',
    'TSG ZF003-2011': 'TSG 92-2026',
    # 检验/检测机构核准（TSG Z7001—2021 / TSG Z7002—2022）
    'TSG Z7001-2004': 'TSG Z7001-2021',
    'TSG Z7002-2004': 'TSG Z7001-2021',
    'TSG Z7003-2004': 'TSG Z7001-2021',
    'TSG Z7005-2015': 'TSG Z7002-2022',
    # 使用管理（TSG 08—2026）
    'TSG 08-2017': 'TSG 08-2026',
    # 同号换版（另有"同号取新"规则自动兜底，此处显式列出主要几项）
    'TSG Z6002-2010': 'TSG Z6002-2026',
    'TSG Z8002-2013': 'TSG Z8002-2022',
    'TSG D7002-2006': 'TSG D7002-2023',
    'TSG D7003-2010': 'TSG D7003-2022',
    'TSG T7007-2016': 'TSG T7007-2022',
}

# 无编号旧文件（试行版等）→ 替代它的新版规程，按名称关键词判定
_TSG_SUPERSEDED_NAMES = {
    '游乐设施安全技术监察规程': 'TSG 71-2023',
    '滑索安全技术要求': 'TSG 71-2023',
    '机电类特种设备安装改造维修许可规则': 'TSG 07-2019',
}


def _split_tsg_series(code: str) -> tuple:
    """拆分 TSG 编号的系列号与年份：'TSG Z8002-2013' → ('TSG Z8002', 2013)"""
    m = re.match(r'TSG\s*([A-Z]{0,2}\d{1,4})-(\d{4})$', code or '')
    if not m:
        return '', 0
    return f'TSG {m.group(1)}', int(m.group(2))


def _mark_superseded_status(standards: list) -> None:
    """就地标记已被新版规范替代的条目为"废止"（SAMR 归档栏目不提供状态）。

    判定依据：
    1. 旧编号命中已核实的替代清单 _TSG_SUPERSEDED；
    2. 同一编号系列存在更新年份（如 TSG Z8002-2013 被 Z8002-2022 取代，自动兜底）；
    3. 无编号旧文件命中名称关键词 _TSG_SUPERSEDED_NAMES。
    """
    latest_year = {}
    for it in standards:
        series, year = _split_tsg_series(it.get('code', ''))
        if series and year > latest_year.get(series, 0):
            latest_year[series] = year

    for it in standards:
        if it.get('status') == '废止':
            continue
        code = (it.get('code') or '').strip()
        name = it.get('name') or ''
        if code in _TSG_SUPERSEDED:
            it['status'] = '废止'
            continue
        name_flat = name.replace(' ', '')
        if any(kw in name_flat for kw in _TSG_SUPERSEDED_NAMES):
            it['status'] = '废止'
            continue
        series, year = _split_tsg_series(code)
        if series and latest_year.get(series, 0) > year:
            it['status'] = '废止'


def _parse_list_items(html: str, source: str = 'aqjsgf') -> list:
    """解析 API 返回的列表 HTML，返回条目列表。"""
    items = []
    tid = _SOURCE_TID.get(source, 'BV_SAMR')
    col = 'flfg' if source == 'flfg' else 'aqjsgf'
    list_base = f'{SAMR_BASE}/tzsbj/zcfg/{col}/index.html'

    for m in _LIST_ITEM_RE.finditer(html):
        href = m.group(1).strip()
        title = m.group(2).strip()
        date = m.group(3).strip()

        # 解析文章 ID
        article_id = None
        id_match = _ARTICLE_ID_RE.search(href)
        if id_match:
            article_id = id_match.group(1)
        else:
            # 直接 PDF 链接：取文件名作为 ID
            pdf_match = _PDF_FILENAME_RE.search(href)
            if pdf_match:
                article_id = pdf_match.group(1)

        if not article_id:
            continue

        # 判断是否为直接 PDF 链接
        is_direct_pdf = href.lower().endswith('.pdf')
        detail_url = None
        pdf_url = None
        if is_direct_pdf:
            pdf_url = urllib.parse.urljoin(SAMR_BASE, href)
        else:
            detail_url = urllib.parse.urljoin(list_base, href)

        # 解析 code 和 name
        code, name = _parse_tsg_title(title)
        status = _parse_status(title)

        item = {
            'detail_id': article_id,
            'pid': article_id,
            'code': code,
            'name': name,
            'publishDate': date,
            'status': status,
            'sellable': False,
            'tid': tid,
            'type': 'mem',
            'source': source,
            'site': 'samr',
            'detail_url': detail_url,
        }
        if pdf_url:
            item['pdf_url'] = pdf_url
            item['file_ext'] = '.pdf'
        items.append(item)
    return items


# ==================== 详情页 PDF 提取 ====================
# 模式1：附件下载区 <ul class="contentLeft0102box"><li class="contentLeft0102 contentLeft0103"><a href="...">
_ATTACH_LINK_RE = re.compile(
    r'<li\s+class="contentLeft0102[^"]*">\s*<a\s+href="([^"]+)"',
    re.IGNORECASE,
)

# 模式2：规章下载区 <div class="gzdownloadcon"><a href="...">下载pdf版</a>
_GZ_PDF_LINK_RE = re.compile(
    r'<div\s+class="gzdownloadcon">\s*<a\s+href="([^"]+)"[^>]*>下载pdf版</a>',
    re.IGNORECASE,
)
# 任意 PDF 链接（回退）
_ANY_PDF_RE = re.compile(r'href="([^"]+\.pdf)"', re.IGNORECASE)


def _fetch_detail_pdf_url(detail_url: str, source: str = 'aqjsgf') -> tuple:
    """从详情页 HTML 提取 PDF URL。返回 (pdf_url, file_ext) 或 (None, None)。"""
    try:
        resp = http_client.get(detail_url, headers=_samr_headers(source), timeout=30)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        _log.error(f"[SAMR][{source}] 详情页请求失败 ({detail_url}): {e}")
        return None, None

    # 优先匹配附件下载区（aqjsgf 常用）
    m = _ATTACH_LINK_RE.search(html)
    if m:
        url = m.group(1)
        # 链接常带 ?fileName= 查询串，需取路径部分再判断扩展名
        ext = '.pdf' if urllib.parse.urlsplit(url).path.lower().endswith('.pdf') else '.docx'
        return _resolve_url(url, detail_url), ext

    # 其次匹配规章下载区 PDF 版（flfg 常用）
    m = _GZ_PDF_LINK_RE.search(html)
    if m:
        return _resolve_url(m.group(1), detail_url), '.pdf'

    # 回退：任意 PDF 链接
    m = _ANY_PDF_RE.search(html)
    if m:
        return _resolve_url(m.group(1), detail_url), '.pdf'

    return None, None


def _resolve_url(url: str, base: str) -> str:
    """相对 URL → 绝对 URL。"""
    if url.startswith('http'):
        return url
    if url.startswith('/'):
        return urllib.parse.urljoin(SAMR_BASE, url)
    return urllib.parse.urljoin(base, url)


# ==================== 扫描 ====================
async def _scan_one_source(
    source: str,
    max_results: int,
    incr: bool,
    is_all: bool,
    status: str,
    on_progress: Callable,
    on_intermediate: Callable,
    check_pause: Callable,
    progress_share: float = 1.0,
    progress_base: int = 0,
) -> list:
    """扫描单个 SAMR 源（flfg 或 aqjsgf）。

    Args:
        source: 'flfg' | 'aqjsgf'
        max_results: 最大扫描条数
        incr: 增量扫描（用文章 ID 比对）
        is_all: 是否跳过关键词过滤
        status: 状态筛选（现行/废止/全部）
        on_progress: async callable(pct, msg)
        on_intermediate: async callable(standards_list)
        check_pause: async callable() → bool
        progress_share: 进度占比
        progress_base: 进度起始值

    Returns:
        标准列表
    """
    all_standards = []
    total_scanned = 0
    ckpt_key = f'mem_samr_{source}_all'
    source_label = _SOURCE_LABEL.get(source, source)

    # 增量 checkpoint
    last_first_id = None
    if incr:
        ckpt = get_incr_checkpoint('mem', ckpt_key)
        if ckpt:
            last_first_id = ckpt.get('first_id')
            _log.info(f"[SAMR][{source}] 增量扫描，上次最新文章 ID={last_first_id}")

    page_no = 1
    total_count = None
    hit_last = False

    while total_scanned < max_results:
        if check_pause:
            try:
                if not await check_pause():
                    return all_standards
            except Exception:
                pass

        if on_progress:
            # 用总条数估算进度（无总条数时按已扫描数估算）
            if total_count and total_count > 0:
                raw_pct = min(95, int(total_scanned / min(max_results, total_count) * 95))
            else:
                raw_pct = min(95, int(total_scanned / max(max_results, 1) * 95))
            pct = progress_base + int(progress_share * raw_pct)
            try:
                await on_progress(pct, f'[{source_label}]第{page_no}页（已{total_scanned}条）')
            except Exception:
                pass

        # 同步 API 调用包装到 executor
        loop = asyncio.get_running_loop()
        try:
            html, tc = await loop.run_in_executor(None, _call_list_api, page_no, source)
            if total_count is None:
                total_count = tc
                _log.info(f"[SAMR][{source}] 总条数: {total_count}")
        except Exception as e:
            _log.error(f"[SAMR][{source}] 第{page_no}页列表请求失败: {e}")
            break

        page_items = _parse_list_items(html, source)
        if not page_items:
            _log.info(f"[SAMR][{source}] 第{page_no}页无数据，结束")
            break

        # 增量比对 + 过滤
        for it in page_items:
            if last_first_id and it['detail_id'] == last_first_id:
                _log.info(f"[SAMR][{source}] 增量命中上次位置 article_id={last_first_id}，停止")
                hit_last = True
                break
            # 关键词过滤（__all__ 模式跳过）
            if not is_all and not is_safety(it.get('name', ''), 'mem'):
                continue
            all_standards.append(it)
            total_scanned += 1
            if total_scanned >= max_results:
                break

        if hit_last:
            break
        if total_scanned >= max_results:
            break

        # 推送中间结果（先做旧版标记，保证界面状态正确）
        if on_intermediate:
            try:
                _mark_superseded_status(all_standards)
                await on_intermediate(list(all_standards))
            except Exception:
                pass

        # 翻页前判断：当前页不足一页，说明已到末页
        if len(page_items) < SAMR_PAGE_SIZE:
            break

        page_no += 1
        # 保护网站
        await asyncio.sleep(get_delay())

    # 状态后处理：归档栏目不提供废止标记，按替代清单/同号取新标记旧版
    _mark_superseded_status(all_standards)
    # 状态过滤放到标记之后（"废止"只能在全量比较后确定）
    if status and status != '全部':
        all_standards = [it for it in all_standards if it.get('status') == status]

    # 更新 checkpoint
    if all_standards:
        update_incr_checkpoint('mem', ckpt_key, {
            'first_id': all_standards[0]['detail_id'],
            'first_code': all_standards[0].get('code', ''),
            'first_name': all_standards[0].get('name', ''),
            'count': len(all_standards),
        })

    _log.info(f"[SAMR][{source}] {source_label}扫描完成: {len(all_standards)} 条")
    return all_standards


async def scan_samr_standards(
    max_results=500,
    incr=False,
    keyword_group=None,
    on_progress: Callable = None,
    on_intermediate: Callable = None,
    check_pause: Callable = None,
    status: str = '现行',
    source: str = 'aqjsgf',
) -> list:
    """扫描市场监管总局政策法规列表（httpx 直连 jpaas API）。

    Args:
        max_results: 最大扫描条数
        incr: 增量扫描（用文章 ID 比对，命中上次位置即停）
        keyword_group: 关键词组名（'__all__'=不过滤）
        on_progress: async callable(pct, msg)
        on_intermediate: async callable(standards_list)
        check_pause: async callable() → bool
        status: 状态筛选（现行/废止/全部）
        source: 'flfg' 法律法规 | 'aqjsgf' 安全技术规范 | 'all' 两源并发扫描后合并

    Returns:
        标准列表
    """
    set_active_group(keyword_group or '安全生产')
    is_all = keyword_group == '__all__'

    # 并发扫描 flfg + aqjsgf，合并结果
    if source == 'all':
        flfg_holder = []
        aqjsgf_holder = []

        async def _flfg_progress(pct, msg):
            if on_progress:
                try:
                    await on_progress(pct // 2, msg)
                except Exception:
                    pass

        async def _aqjsgf_progress(pct, msg):
            if on_progress:
                try:
                    await on_progress(50 + pct // 2, msg)
                except Exception:
                    pass

        async def _flfg_intermediate(items):
            flfg_holder.clear()
            flfg_holder.extend(items)
            if on_intermediate:
                try:
                    await on_intermediate(flfg_holder + aqjsgf_holder)
                except Exception:
                    pass

        async def _aqjsgf_intermediate(items):
            aqjsgf_holder.clear()
            aqjsgf_holder.extend(items)
            if on_intermediate:
                try:
                    await on_intermediate(flfg_holder + aqjsgf_holder)
                except Exception:
                    pass

        flfg_std, aqjsgf_std = await asyncio.gather(
            _scan_one_source('flfg', max_results, incr, is_all, status,
                             _flfg_progress, _flfg_intermediate, check_pause,
                             progress_share=0.5, progress_base=0),
            _scan_one_source('aqjsgf', max_results, incr, is_all, status,
                             _aqjsgf_progress, _aqjsgf_intermediate, check_pause,
                             progress_share=0.5, progress_base=50),
        )
        all_standards = flfg_std + aqjsgf_std
        _log.info(f"[SAMR][all] 两源合并: flfg={len(flfg_std)} + aqjsgf={len(aqjsgf_std)} = {len(all_standards)} 条")
        return all_standards

    # 单源扫描
    return await _scan_one_source(
        source, max_results, incr, is_all, status,
        on_progress, on_intermediate, check_pause,
    )


# ==================== 下载 ====================
async def _download_one_standard(std: dict, existing: set) -> dict:
    """下载单个市场监管总局法规文档。返回更新后的 std（含 dlStatus / failReason）。"""
    source = std.get('source', 'aqjsgf')
    code = std.get('code', '')
    name = std.get('name', '')
    file_ext = std.get('file_ext') or '.pdf'

    filename = make_filename(code, name, suffix=file_ext)
    if filename.lower() in existing:
        std[_SAMR_DL_STATUS] = 'skipped_existing'
        return std

    # 取下载 URL
    pdf_url = std.get('pdf_url')
    if not pdf_url:
        detail_url = std.get('detail_url')
        if not detail_url:
            _log.info(f"  [SAMR][{source}][SKIP] 无详情 URL: {code} {name}")
            std[_SAMR_DL_STATUS] = 'no_fulltext'
            std['failReason'] = '无详情URL'
            return std
        loop = asyncio.get_running_loop()
        pdf_url, file_ext = await loop.run_in_executor(
            None, _fetch_detail_pdf_url, detail_url, source)
        if file_ext:
            std['file_ext'] = file_ext
            # 重新生成文件名（后缀可能从 .pdf 变为 .docx）
            filename = make_filename(code, name, suffix=file_ext)
            if filename.lower() in existing:
                std[_SAMR_DL_STATUS] = 'skipped_existing'
                return std

    if not pdf_url:
        _log.info(f"  [SAMR][{source}][SKIP] 无下载链接: {code} {name}")
        std[_SAMR_DL_STATUS] = 'no_fulltext'
        std['failReason'] = '无下载链接'
        return std

    # 下载文档
    loop = asyncio.get_running_loop()
    try:
        resp = await loop.run_in_executor(
            None,
            lambda: http_client.get(pdf_url, headers=_samr_headers(source), timeout=60),
        )
        content_type = resp.headers.get('content-type', '')
        body = resp.content
        if file_ext == '.pdf':
            is_pdf = 'pdf' in content_type.lower() or body[:5] == b'%PDF-'
            if not is_pdf or len(body) < 1000:
                reason = f'响应非PDF(ct={content_type},len={len(body)}B)'
                _log.info(f"  [SAMR][{source}][FAIL] {reason}: {code}")
                std[_SAMR_DL_STATUS] = 'failed'
                std['failReason'] = reason
                return std
        elif len(body) < 1000:
            reason = f'响应过小(ct={content_type},len={len(body)}B)'
            _log.info(f"  [SAMR][{source}][FAIL] {reason}: {code}")
            std[_SAMR_DL_STATUS] = 'failed'
            std['failReason'] = reason
            return std
        # 源站偶发：链接带查询串导致扩展名误判为 .docx，实际内容是 PDF，按魔数修正
        if file_ext != '.pdf' and body[:5] == b'%PDF-':
            file_ext = '.pdf'
            std['file_ext'] = file_ext
            filename = make_filename(code, name, suffix=file_ext)
            if filename.lower() in existing:
                std[_SAMR_DL_STATUS] = 'skipped_existing'
                return std
        # 落盘
        out_dir = get_output_dir()
        filepath = out_dir / filename
        filepath.write_bytes(body)
        _log.info(f"  [SAMR][{source}][OK] {filepath} ({len(body)/1024:.0f}KB)")
        std[_SAMR_DL_STATUS] = 'downloaded'
        std.pop('failReason', None)
        existing.add(filename.lower())
        return std
    except Exception as e:
        reason = f'{type(e).__name__}: {e}'
        _log.error(f"  [SAMR][{source}][ERROR] 下载失败 {code}: {e}")
        std[_SAMR_DL_STATUS] = 'failed'
        std['failReason'] = reason
        return std


async def download_samr_standards(
    standards: list,
    on_progress: Callable = None,
    on_item_done: Callable = None,
    check_pause: Callable = None,
):
    """下载市场监管总局法规列表（httpx 直接调用）。

    并发：读 config.download.concurrent（默认 1）。
    SAMR 为纯 PDF/DOCX 下载，无 cookie session 依赖，使用共享 http_client 即可。
    """
    from app.dedup import get_existing_files
    from config.manager import load_config
    existing = get_existing_files()
    total = len(standards)

    concurrent = max(1, min(10, int(load_config().get('download', {}).get('concurrent', 1))))
    if concurrent > 1:
        _log.info(f"[SAMR-DL] 并发度: {concurrent}")

    done_counter = [0]
    stats_lock = asyncio.Lock()

    async def _report_done(item_name=''):
        async with stats_lock:
            done_counter[0] += 1
            idx = done_counter[0]
            if on_item_done:
                await on_item_done(item_name)
            if on_progress and total > 0:
                await on_progress(int(100 * idx / total), f"下载中 {idx}/{total}")

    sem = asyncio.Semaphore(concurrent)

    async def _do_one(std):
        async with sem:
            if check_pause:
                try:
                    if not await check_pause():
                        return
                except Exception:
                    pass

            code = std.get('code', '')
            name = std.get('name', '')
            source = std.get('source', 'aqjsgf')
            _log.info(f"[SAMR][{source}] 处理: {code} {name}".strip())

            await _download_one_standard(std, existing)

            await _report_done(f"{code} {name[:20]}".strip())

            # 成功下载后不 sleep（与 MEM 一致），失败后 sleep
            if std.get(_SAMR_DL_STATUS) == 'failed':
                await asyncio.sleep(get_delay())

    if standards:
        await asyncio.gather(*[_do_one(s) for s in standards])


# ==================== 统一管线 ====================
async def run_samr_pipeline(
    config: dict,
    task_id: str = None,
    task_manager=None,
    progress_base: int = 0,
    progress_per_scan: int = 40,
    progress_per_download: int = 60,
) -> list:
    """市场监管总局政策法规统一管线：scan + download，全部基于 httpx。"""
    from app.scanner.change_tracker import compare_snapshot
    from app.scanner.utils import compute_download_stats

    max_results = config.get('max_results', 500)
    incr = config.get('incr', False)
    keyword_group = config.get('keyword_group', '安全生产')
    scan_only = config.get('scan_only', False)
    std_state = config.get('std_state', '现行')
    source = config.get('source', 'aqjsgf')
    source_label = {'flfg': '法律法规', 'aqjsgf': '安全技术规范',
                    'all': '法律法规+安全技术规范'}.get(source, '安全技术规范')

    # === 进度回调 ===
    async def _on_scan_progress(pct, msg):
        if task_manager and task_id:
            scaled = progress_base + max(1, int(pct * progress_per_scan / 100))
            task_manager.update(task_id, progress=scaled, message=msg, persist_std_items=False)

    async def _on_dl_progress(pct, msg):
        if task_manager and task_id:
            scaled = progress_base + progress_per_scan + max(1, int(pct * progress_per_download / 100))
            task_manager.update(task_id, progress=scaled, message=msg, persist_std_items=False)

    async def _check_pause():
        if not (task_manager and task_id):
            return True
        while True:
            task = task_manager.get(task_id)
            if not task:
                return False
            status = task.get('status')
            if status == 'running':
                return True
            if status != 'paused':
                return False
            await asyncio.sleep(1)

    _intermediate_counter = [0]

    async def _on_intermediate(standards_list):
        if task_manager and task_id:
            _intermediate_counter[0] += 1
            persist = _intermediate_counter[0] % 3 == 0
            task_manager.update(task_id, std_items=list(standards_list),
                                stats={'scanned': len(standards_list)}, persist_std_items=persist)

    _item_counter = [0]
    standards_holder = []

    async def _on_item_done(item_name=''):
        _item_counter[0] += 1
        if task_manager and task_id:
            new_stats = compute_download_stats(standards_holder[-1] if standards_holder else [])
            kwargs = {'stats': new_stats, 'persist_std_items': False}
            if _item_counter[0] % 5 == 0:
                kwargs['std_items'] = standards_holder[-1] if standards_holder else []
                kwargs['persist_std_items'] = True
            if item_name:
                kwargs['currentItem'] = item_name
            task_manager.update(task_id, **kwargs)

    # === 阶段1: 扫描 ===
    if task_manager and task_id:
        task_manager.update(task_id, progress=progress_base + 1,
                            scan_progress=0, message=f'扫描市场监管总局{source_label}...')

    standards = await scan_samr_standards(
        max_results=max_results, incr=incr,
        keyword_group=keyword_group, on_progress=_on_scan_progress,
        on_intermediate=_on_intermediate, check_pause=_check_pause, status=std_state,
        source=source,
    )
    standards_holder.append(standards)

    if task_manager and task_id:
        task_manager.update(task_id, progress=progress_base + progress_per_scan,
                            scan_progress=100, message=f'市场监管总局{source_label}扫描完成({len(standards)}条)',
                            stats={'scanned': len(standards)}, std_items=standards)

    _log.info(f"[SAMR][{source}] {source_label}扫描完成: {len(standards)} 条")

    # 变更追踪
    try:
        changes = compare_snapshot('mem', standards)
        if task_manager and task_id and (changes.get('added') or changes.get('changed') or changes.get('removed')):
            task_manager.update(task_id, changes=changes)
    except Exception as e:
        _log.debug(f"[SAMR][{source}] 变更追踪异常: {e}")

    # 扫描结果为空：跳过下载阶段
    if not standards:
        if task_manager and task_id:
            state_label = std_state or '全部'
            empty_msg = '无新增' if incr else '无符合条件'
            task_manager.update(task_id,
                progress=progress_base + progress_per_scan + progress_per_download,
                dl_progress=100,
                message=f"{empty_msg}{source_label}（{state_label}），跳过下载",
                stats={'scanned': 0, 'downloaded': 0, 'success': 0, 'failed': 0, 'skipped': 0},
                std_items=[])
        _log.info(f"[SAMR][{source}] {empty_msg}{source_label}（{std_state or '全部'}），跳过下载")
        return standards

    if scan_only:
        return standards

    # === 阶段2: 下载 ===
    if task_manager and task_id:
        task_manager.update(task_id, progress=progress_base + progress_per_scan + 1,
                            dl_progress=0, message=f'下载市场监管总局{source_label}...')

    await download_samr_standards(standards,
                                  on_progress=_on_dl_progress,
                                  on_item_done=_on_item_done,
                                  check_pause=_check_pause)

    if task_manager and task_id:
        task_manager.update(task_id,
                            progress=progress_base + progress_per_scan + progress_per_download,
                            dl_progress=100, message=f'市场监管总局{source_label}下载完成({len(standards)}条)',
                            stats=compute_download_stats(standards), std_items=standards)

    _log.info(f"[SAMR][{source}] {source_label}下载完成: {len(standards)} 条")
    return standards
