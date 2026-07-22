"""scanner.mem_scan — 应急管理部标准/规章扫描与下载

应急管理部 - 法律法规标准 下两个栏目：

1) 标准文本 (bz)：https://www.mem.gov.cn/fw/flfgbz/bz/bzwb/
   - HTML 静态渲染，每页 20 条，共 48 页 / 948 条
   - 列表项：<a class="newttle" href="...">TITLE<span>DATE</span></a>
   - 详情页：在 <div class="zhenwen_neir"> 内有 <a href="...PDF_URL..."> 标准名 </a>
   - 标题格式："CODE—YEAR NAME"（如 "AQ 9010.4—2026 安全生产..."）
     部分标题带《》书名号："GB 23468-2025《 坠落防护...》"
   - 状态：mem.gov.cn 不提供，所有按"现行"处理

2) 规章 (gz)：https://www.mem.gov.cn/fw/flfgbz/gz/  (列表通过 iframe 加载)
   iframe URL：https://www.mem.gov.cn/gk/zfxxgkpt/fdzdgknr/gz11/index.shtml
   - HTML 静态渲染，表格结构（序号|标题|下载链接）
   - 每页 15 条，共 5 页 / 74 条
   - 下载链接直接在列表页（无需访问详情页）
     每条提供"下载文字版"(.pdf/.docx/.doc/.wps) 与"下载图片版"(.pdf) 两个链接
     本模块优先下载"图片版" PDF，无 PDF 时回退到 .docx/.doc
   - 标题为规章名称（无标准号），可从名称识别"已废止"状态

分页规律（两源一致）：
  page_no=1 → 基础 URL
  page_no=N (N>=2) → index_{N-1}.shtml

字段映射（mem.gov.cn → 项目内部统一字段）：
  文章 ID（URL 末尾数字，如 608470） → detail_id / pid
  bz: 标题前半部分（如 "AQ 9010.4-2026"） → code；标题后半部分 → name
  gz: code 留空；name = 规章标题
  bz: 状态固定 '现行'
  gz: 标题含"已废止" → '废止'，否则 '现行'

增量扫描：用文章 ID 作为比对字段。
"""

import asyncio
import logging
import re
import urllib.parse
from typing import Callable, Optional

from app.helpers import normalize_code, _DASH_NORMALIZE_TABLE
from app.keywords import is_safety, set_active_group
from app.scanner.checkpoint import get_incr_checkpoint, update_incr_checkpoint
from app.scanner.utils import make_filename
from config.settings import (
    MEM_BASE, MEM_LIST_URL, MEM_GZ_LIST_URL,
    get_output_dir, get_delay, http_client,
)

_log = logging.getLogger('std_scraper')

# ==================== 常量 ====================
_MEM_PAGE_SIZE = 20                # bz 列表每页条数（实测）
_MEM_GZ_PAGE_SIZE = 15             # gz 列表每页条数（实测）
_MEM_DL_STATUS = 'dlStatus'        # std_items 里的下载状态字段（与 GB/HB/DB/TT 一致）
_MEM_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
           '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

# 源 → 列表基础 URL
_SOURCE_LIST_URL = {
    'bz': MEM_LIST_URL,
    'gz': MEM_GZ_LIST_URL,
}

# 源 → 每页条数
_SOURCE_PAGE_SIZE = {
    'bz': _MEM_PAGE_SIZE,
    'gz': _MEM_GZ_PAGE_SIZE,
}


def _mem_headers(source: str = 'bz') -> dict:
    """构造 mem.gov.cn 请求头。"""
    return {
        'User-Agent': _MEM_UA,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Referer': _SOURCE_LIST_URL.get(source, MEM_LIST_URL),
    }


def _page_url(page_no: int, source: str = 'bz') -> str:
    """根据页码构造列表页 URL（1-indexed）。

    实测规则（bz 与 gz 一致）：
      page_no=1 → 基础 URL
      page_no=N (N>=2) → index_{N-1}.shtml
    """
    base = _SOURCE_LIST_URL.get(source, MEM_LIST_URL)
    if page_no <= 1:
        return base
    return f"{base}index_{page_no - 1}.shtml"


# ==================== bz 源：标准文本列表解析 ====================
# 列表项：<a class="newttle" href="...">TITLE<span>DATE</span></a>
_BZ_LIST_ITEM_RE = re.compile(
    r'<a\s+class="newttle"\s+href="([^"]+)"\s*>([^<]+)<span>([^<]+)</span></a>'
)

# 详情页 PDF URL 正则（取第一个 .pdf 链接）
_BZ_DETAIL_PDF_RE = re.compile(r'href="((?:https?://[^"]*?|/[^"]*?)\.pdf)"', re.IGNORECASE)

# 标题解析：CODE—YEAR NAME
# CODE 形如：AQ / AQ 9010.4 / YJT 48 / YJ/T 41
# 先做 dash 归一化（— → -），再用前缀正则一次性分离 code/name
_BZ_CODE_PREFIX_RE = re.compile(r'^([A-Z]+(?:/[A-Z])?\s+\d+(?:\.\d+)*-\d{4})')

# 文章 ID 提取：t20260622_608470.shtml → 608470
_ARTICLE_ID_RE = re.compile(r't\d{8}_(\d+)\.shtml')


def _bz_parse_title(title: str) -> tuple:
    """解析 bz 标题为 (code, name)。

    示例：
      'AQ 9010.4—2026 安全生产责任保险事故预防服务规范 第4部分：矿山'
        → ('AQ 9010.4-2026', '安全生产责任保险事故预防服务规范 第4部分：矿山')
      'GB 23468-2025《 坠落防护装备的选择、使用和维护》'
        → ('GB 23468-2025', '坠落防护装备装备的选择、使用和维护')
      'YJ/T 40—2026 自然灾害灾情社会化采集技术规范'
        → ('YJ/T 40-2026', '自然灾害灾情社会化采集技术规范')
    """
    # 先归一化 dash 字符（— → -）
    normalized = title.translate(_DASH_NORMALIZE_TABLE)
    m = _BZ_CODE_PREFIX_RE.match(normalized)
    if m:
        code = normalize_code(m.group(1))
        name = normalized[m.end():].strip()
        # 剥离《》书名号（部分标题用书名号包裹名称）
        if name.startswith('《'):
            name = name[1:].strip()
        if name.endswith('》'):
            name = name[:-1].strip()
        return code, name
    # 无法解析 code：整段作为 name，code 留空
    return '', title.strip()


def _bz_parse_list_items(html: str) -> list:
    """解析 bz 列表页 HTML，返回标准条目列表。"""
    items = []
    for m in _BZ_LIST_ITEM_RE.finditer(html):
        href, title, date = m.group(1), m.group(2).strip(), m.group(3).strip()

        # 解析文章 ID（作为 detail_id 和 pid）
        id_match = _ARTICLE_ID_RE.search(href)
        if not id_match:
            continue
        article_id = id_match.group(1)

        # 解析 code 和 name
        code, name = _bz_parse_title(title)

        # 详情页 URL（相对路径 → 绝对路径）
        detail_url = urllib.parse.urljoin(MEM_LIST_URL, href)

        # 日期取 YYYY-MM-DD 部分（原格式 "2026-06-22 10:43"）
        publish_date = date.split(' ')[0] if ' ' in date else date

        items.append({
            'detail_id': article_id,
            'pid': article_id,
            'code': code,
            'name': name,
            'publishDate': publish_date,
            'status': '现行',          # mem.gov.cn 不提供状态，新发布按现行处理
            'sellable': False,
            'tid': 'BV_MEM',
            'type': 'mem',
            'source': 'bz',
            'detail_url': detail_url,
        })
    return items


# ==================== gz 源：规章列表解析 ====================
# 表格行正则：抓取三段（序号 td、标题 td、下载 td）
# 用 .*? 配合 re.DOTALL 跨行匹配
_GZ_ROW_RE = re.compile(
    r'<td><div[^>]*>\s*(\d+)\s*</div></td>\s*'
    r'<td class="eve">(.*?)</td>\s*'
    r'<td>(.*?)</td>',
    re.DOTALL,
)

# 从标题 td 中提取详情 URL 和标题
_GZ_TITLE_RE = re.compile(
    r'<a\s+href="([^"]+)"[^>]*>([^<]+)</a>'
)

# 从下载 td 中提取所有链接
_GZ_DL_LINK_RE = re.compile(
    r'<a\s+href="([^"]+)"[^>]*>([^<]+)</a>',
    re.IGNORECASE,
)


def _gz_parse_status(title: str) -> str:
    """根据规章标题判断状态。"""
    if '已废止' in title or '（已废止）' in title:
        return '废止'
    return '现行'


def _gz_pick_download_url(links: list) -> tuple:
    """从下载链接列表中挑选最佳下载 URL。

    links: [(url, label), ...]
    返回: (url, ext) 或 (None, None)

    策略：优先取"图片版"PDF；若无则取"文字版"PDF；
    再无则取任意 PDF；再无则回退到 .docx / .doc（Word 文字版）。
    """
    if not links:
        return None, None
    # 1) 图片版（必为 PDF）
    for url, label in links:
        if '图片版' in label and url.lower().endswith('.pdf'):
            return url, '.pdf'
    # 2) 文字版 PDF
    for url, label in links:
        if '文字版' in label and url.lower().endswith('.pdf'):
            return url, '.pdf'
    # 3) 任意 .pdf
    for url, label in links:
        if url.lower().endswith('.pdf'):
            return url, '.pdf'
    # 4) 文字版 .docx
    for url, label in links:
        if '文字版' in label and url.lower().endswith('.docx'):
            return url, '.docx'
    # 5) 任意 .docx
    for url, label in links:
        if url.lower().endswith('.docx'):
            return url, '.docx'
    # 6) 文字版 .doc
    for url, label in links:
        if '文字版' in label and url.lower().endswith('.doc'):
            return url, '.doc'
    # 7) 任意 .doc
    for url, label in links:
        if url.lower().endswith('.doc'):
            return url, '.doc'
    return None, None


def _gz_parse_list_items(html: str) -> list:
    """解析 gz 列表页 HTML，返回规章条目列表。"""
    items = []
    for m in _GZ_ROW_RE.finditer(html):
        title_html, dl_html = m.group(2), m.group(3)

        # 提取详情 URL 和标题
        title_m = _GZ_TITLE_RE.search(title_html)
        if not title_m:
            continue
        href = title_m.group(1).strip()
        title = title_m.group(2).strip()

        # 解析文章 ID（作为 detail_id 和 pid）
        id_match = _ARTICLE_ID_RE.search(href)
        if not id_match:
            continue
        article_id = id_match.group(1)

        # 详情页 URL（相对路径 → 绝对路径）
        detail_url = urllib.parse.urljoin(MEM_GZ_LIST_URL, href)

        # 提取所有下载链接
        links = [(u.strip(), lbl.strip()) for u, lbl in _GZ_DL_LINK_RE.findall(dl_html)]
        pdf_url, file_ext = _gz_pick_download_url(links)

        # 规章无标准号：code 留空，name 即标题
        # 状态从标题识别（"已废止"）
        status = _gz_parse_status(title)

        items.append({
            'detail_id': article_id,
            'pid': article_id,
            'code': '',                  # 规章无标准号
            'name': title,
            'publishDate': '',           # gz 列表页不直接给日期（在副标题中）
            'status': status,
            'sellable': False,
            'tid': 'BV_MEM_GZ',
            'type': 'mem',
            'source': 'gz',
            'detail_url': detail_url,
            'pdf_url': pdf_url,          # gz 列表页已有下载 URL（无需访问详情页）
            'file_ext': file_ext,        # 下载文件后缀（.pdf/.docx/.doc）
        })
    return items


# ==================== 列表/详情页请求 ====================
def _call_list_page(page_no: int, source: str = 'bz') -> str:
    """同步获取列表页 HTML。"""
    url = _page_url(page_no, source)
    resp = http_client.get(url, headers=_mem_headers(source), timeout=30)
    resp.raise_for_status()
    return resp.text


def _call_detail_page(detail_url: str, source: str = 'bz') -> str:
    """同步获取详情页 HTML。"""
    resp = http_client.get(detail_url, headers=_mem_headers(source), timeout=30)
    resp.raise_for_status()
    return resp.text


def _parse_list_items(html: str, source: str = 'bz') -> list:
    """根据 source 派发到对应解析器。"""
    if source == 'gz':
        return _gz_parse_list_items(html)
    return _bz_parse_list_items(html)


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
    """扫描单个源（bz 或 gz）。

    Args:
        source: 'bz' | 'gz'
        max_results: 最大扫描条数
        incr: 增量扫描（用文章 ID 比对）
        is_all: 是否跳过关键词过滤
        status: 状态筛选（现行/废止/全部）
        on_progress: async callable(pct, msg)
        on_intermediate: async callable(standards_list)
        check_pause: async callable() → bool
        progress_share: 进度占比（单源=1.0，多源并发=0.5）
        progress_base: 进度起始值（多源并发时偏移）

    Returns:
        标准列表
    """
    all_standards = []
    total_scanned = 0
    ckpt_key = f'mem_{source}_all'
    page_size = _SOURCE_PAGE_SIZE.get(source, _MEM_PAGE_SIZE)
    source_label = '规章' if source == 'gz' else '标准文本'

    # 增量 checkpoint
    last_first_id = None
    if incr:
        ckpt = get_incr_checkpoint('mem', ckpt_key)
        if ckpt:
            last_first_id = ckpt.get('first_id')
            _log.info(f"[MEM][{source}] 增量扫描，上次最新文章 ID={last_first_id}")

    page_no = 1
    hit_last = False

    while total_scanned < max_results:
        if check_pause:
            try:
                if not await check_pause():
                    return all_standards
            except Exception:
                pass

        if on_progress:
            raw_pct = min(95, int(total_scanned / max(max_results, 1) * 95))
            pct = progress_base + int(progress_share * raw_pct)
            try:
                await on_progress(pct, f'[{source_label}]第{page_no}页（已{total_scanned}条）')
            except Exception:
                pass

        # 同步 HTTP 调用包装到 executor
        loop = asyncio.get_running_loop()
        try:
            html = await loop.run_in_executor(None, _call_list_page, page_no, source)
        except Exception as e:
            _log.error(f"[MEM][{source}] 第{page_no}页列表请求失败: {e}")
            break

        page_items = _parse_list_items(html, source)
        if not page_items:
            _log.info(f"[MEM][{source}] 第{page_no}页无数据，结束")
            break

        # 增量比对 + 过滤
        for it in page_items:
            if last_first_id and it['detail_id'] == last_first_id:
                _log.info(f"[MEM][{source}] 增量命中上次位置 article_id={last_first_id}，停止")
                hit_last = True
                break
            # 状态过滤
            if status and status != '全部' and it.get('status') != status:
                continue
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

        # 推送中间结果
        if on_intermediate:
            try:
                await on_intermediate(list(all_standards))
            except Exception:
                pass

        # 翻页前判断：当前页不足一页，说明已到末页
        if len(page_items) < page_size:
            break

        page_no += 1
        # 保护网站
        await asyncio.sleep(get_delay())

    # 更新 checkpoint（保存本次最新一条文章 ID）
    if all_standards:
        update_incr_checkpoint('mem', ckpt_key, {
            'first_id': all_standards[0]['detail_id'],
            'first_code': all_standards[0].get('code', ''),
            'first_name': all_standards[0].get('name', ''),
            'count': len(all_standards),
        })

    _log.info(f"[MEM][{source}] {source_label}扫描完成: {len(all_standards)} 条")
    return all_standards


async def scan_mem_standards(
    max_results=500,
    incr=False,
    keyword_group=None,
    on_progress: Callable = None,
    on_intermediate: Callable = None,
    check_pause: Callable = None,
    status: str = '现行',
    source: str = 'bz',
) -> list:
    """扫描应急管理部标准/规章列表（httpx 直接抓取 HTML 列表页）。

    Args:
        max_results: 最大扫描条数
        incr: 增量扫描（用文章 ID 比对，命中上次位置即停）
        keyword_group: 关键词组名（'__all__'=不过滤）
        on_progress: async callable(pct, msg)
        on_intermediate: async callable(standards_list)
        check_pause: async callable() → bool
        status: 状态筛选（现行/废止/全部）。
                bz 不提供状态字段，所有标准按"现行"处理，
                故"废止"返回空，"现行"/"全部"返回全部。
                gz 可从标题识别"已废止"。
        source: 'bz' 标准文本 | 'gz' 规章 | 'all' 两源并发扫描后合并

    Returns:
        标准列表
    """
    set_active_group(keyword_group or '安全生产')
    is_all = keyword_group == '__all__'

    # 并发扫描 bz + gz，合并结果
    if source == 'all':
        bz_holder = []
        gz_holder = []

        async def _bz_progress(pct, msg):
            if on_progress:
                try:
                    await on_progress(pct // 2, msg)
                except Exception:
                    pass

        async def _gz_progress(pct, msg):
            if on_progress:
                try:
                    await on_progress(50 + pct // 2, msg)
                except Exception:
                    pass

        async def _bz_intermediate(items):
            bz_holder.clear()
            bz_holder.extend(items)
            if on_intermediate:
                try:
                    await on_intermediate(bz_holder + gz_holder)
                except Exception:
                    pass

        async def _gz_intermediate(items):
            gz_holder.clear()
            gz_holder.extend(items)
            if on_intermediate:
                try:
                    await on_intermediate(bz_holder + gz_holder)
                except Exception:
                    pass

        bz_std, gz_std = await asyncio.gather(
            _scan_one_source('bz', max_results, incr, is_all, status,
                             _bz_progress, _bz_intermediate, check_pause,
                             progress_share=0.5, progress_base=0),
            _scan_one_source('gz', max_results, incr, is_all, status,
                             _gz_progress, _gz_intermediate, check_pause,
                             progress_share=0.5, progress_base=50),
        )
        all_standards = bz_std + gz_std
        _log.info(f"[MEM][all] 两源合并: bz={len(bz_std)} + gz={len(gz_std)} = {len(all_standards)} 条")
        return all_standards

    # 单源扫描
    return await _scan_one_source(
        source, max_results, incr, is_all, status,
        on_progress, on_intermediate, check_pause,
    )


# ==================== 下载 ====================
def _fetch_bz_pdf_url(detail_url: str) -> Optional[str]:
    """从 bz 详情页 HTML 提取 PDF URL。返回 PDF URL 或 None。"""
    try:
        html = _call_detail_page(detail_url, source='bz')
    except Exception as e:
        _log.error(f"[MEM][bz] 详情页请求失败 ({detail_url}): {e}")
        return None

    m = _BZ_DETAIL_PDF_RE.search(html)
    if not m:
        return None

    pdf_url = m.group(1)
    # 处理相对 URL
    if pdf_url.startswith('/'):
        pdf_url = urllib.parse.urljoin(MEM_BASE, pdf_url)
    elif not pdf_url.startswith('http'):
        pdf_url = urllib.parse.urljoin(detail_url, pdf_url)

    return pdf_url


async def _download_one_standard(std: dict, existing: set) -> dict:
    """下载单个应急管理部标准/规章文档。返回更新后的 std（含 dlStatus）。

    bz: 详情页 → 提取 PDF URL → GET 下载 PDF
    gz: 下载 URL 已在列表项（std['pdf_url']）→ 直接 GET 下载（PDF/DOCX/DOC）
    """
    source = std.get('source', 'bz')
    code = std.get('code', '')
    name = std.get('name', '')
    file_ext = std.get('file_ext') or '.pdf'

    filename = make_filename(code, name, suffix=file_ext)
    if filename in existing:
        std[_MEM_DL_STATUS] = 'skipped_existing'
        return std

    if std.get('sellable'):
        _log.info(f"  [MEM][{source}][SKIP] 付费标准: {code} {name}")
        std[_MEM_DL_STATUS] = 'copyright'
        return std

    # 取下载 URL
    pdf_url = std.get('pdf_url')
    if not pdf_url and source == 'bz':
        detail_url = std.get('detail_url')
        if not detail_url:
            _log.info(f"  [MEM][bz][SKIP] 无详情 URL: {code} {name}")
            std[_MEM_DL_STATUS] = 'no_fulltext'
            return std
        loop = asyncio.get_running_loop()
        pdf_url = await loop.run_in_executor(None, _fetch_bz_pdf_url, detail_url)

    if not pdf_url:
        _log.info(f"  [MEM][{source}][SKIP] 无下载链接: {code} {name}")
        std[_MEM_DL_STATUS] = 'no_fulltext'
        return std

    # 下载文档
    loop = asyncio.get_running_loop()
    try:
        resp = await loop.run_in_executor(
            None,
            lambda: http_client.get(pdf_url, headers=_mem_headers(source), timeout=60),
        )
        content_type = resp.headers.get('content-type', '')
        body = resp.content
        # PDF 校验头；Word 仅校验长度（docx=PK.., doc=D0CF11E0..）
        if file_ext == '.pdf':
            is_pdf = 'pdf' in content_type.lower() or body[:5] == b'%PDF-'
            if not is_pdf or len(body) < 1000:
                _log.info(f"  [MEM][{source}][FAIL] 响应非 PDF ({content_type}, {len(body)}B): {code}")
                std[_MEM_DL_STATUS] = 'failed'
                return std
        elif len(body) < 1000:
            _log.info(f"  [MEM][{source}][FAIL] 响应过小 ({content_type}, {len(body)}B): {code}")
            std[_MEM_DL_STATUS] = 'failed'
            return std
        # 落盘
        out_dir = get_output_dir()
        filepath = out_dir / filename
        filepath.write_bytes(body)
        _log.info(f"  [MEM][{source}][OK] {filepath} ({len(body)/1024:.0f}KB)")
        std[_MEM_DL_STATUS] = 'downloaded'
        existing.add(filename)
        return std
    except Exception as e:
        _log.error(f"  [MEM][{source}][ERROR] 下载失败 {code}: {e}")
        std[_MEM_DL_STATUS] = 'failed'
        return std


async def download_mem_standards(
    standards: list,
    on_progress: Callable = None,
    on_item_done: Callable = None,
    check_pause: Callable = None,
):
    """下载应急管理部标准/规章列表（httpx 直接调用）。

    并发：读 config.download.concurrent（默认 1）。>1 时下载并发执行。
    MEM 为纯 PDF 下载，无 cookie session 依赖，使用共享 http_client 即可。
    """
    from app.dedup import get_existing_files
    from config.manager import load_config
    existing = get_existing_files()
    total = len(standards)

    concurrent = max(1, min(10, int(load_config().get('download', {}).get('concurrent', 1))))
    if concurrent > 1:
        _log.info(f"[MEM-DL] 并发度: {concurrent}")

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
            source = std.get('source', 'bz')
            _log.info(f"[MEM][{source}] 处理: {code} {name}".strip())

            await _download_one_standard(std, existing)

            await _report_done(f"{code} {name[:20]}".strip())

            # 成功下载后不 sleep（与 HB/DB/TT 一致），失败后 sleep
            if std.get(_MEM_DL_STATUS) == 'failed':
                await asyncio.sleep(get_delay())

    if standards:
        await asyncio.gather(*[_do_one(s) for s in standards])


# ==================== 统一管线 ====================
async def run_mem_pipeline(
    config: dict,
    task_id: str = None,
    task_manager=None,
    progress_base: int = 0,
    progress_per_scan: int = 40,
    progress_per_download: int = 60,
) -> list:
    """应急管理部标准/规章统一管线：scan + download，全部基于 httpx。"""
    from app.scanner.change_tracker import compare_snapshot
    from app.scanner.utils import compute_download_stats

    max_results = config.get('max_results', 500)
    incr = config.get('incr', False)
    keyword_group = config.get('keyword_group', '安全生产')
    scan_only = config.get('scan_only', False)
    std_state = config.get('std_state', '现行')
    source = config.get('source', 'bz')
    source_label = {'bz': '标准文本', 'gz': '规章', 'all': '标准文本+规章'}.get(source, '标准文本')

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
                            scan_progress=0, message=f'扫描应急管理部{source_label}...')

    standards = await scan_mem_standards(
        max_results=max_results, incr=incr,
        keyword_group=keyword_group, on_progress=_on_scan_progress,
        on_intermediate=_on_intermediate, check_pause=_check_pause, status=std_state,
        source=source,
    )
    standards_holder.append(standards)

    if task_manager and task_id:
        task_manager.update(task_id, progress=progress_base + progress_per_scan,
                            scan_progress=100, message=f'应急管理部{source_label}扫描完成({len(standards)}条)',
                            stats={'scanned': len(standards)}, std_items=standards)

    _log.info(f"[MEM][{source}] {source_label}扫描完成: {len(standards)} 条")

    # 变更追踪
    try:
        changes = compare_snapshot('mem', standards)
        if task_manager and task_id and (changes.get('added') or changes.get('changed') or changes.get('removed')):
            task_manager.update(task_id, changes=changes)
    except Exception as e:
        _log.debug(f"[MEM][{source}] 变更追踪异常: {e}")

    # 扫描结果为空：明确标识"无符合条件标准"，跳过下载阶段
    if not standards:
        if task_manager and task_id:
            state_label = std_state or '全部'
            task_manager.update(task_id,
                progress=progress_base + progress_per_scan + progress_per_download,
                dl_progress=100,
                message=f"无符合条件{source_label}（{state_label}），跳过下载",
                stats={'scanned': 0, 'downloaded': 0, 'success': 0, 'failed': 0, 'skipped': 0},
                std_items=[])
        _log.info(f"[MEM][{source}] 无符合条件{source_label}（{std_state or '全部'}），跳过下载")
        return standards

    if scan_only:
        return standards

    # === 阶段2: 下载 ===
    if task_manager and task_id:
        task_manager.update(task_id, progress=progress_base + progress_per_scan + 1,
                            dl_progress=0, message=f'下载应急管理部{source_label}...')

    await download_mem_standards(standards,
                                 on_progress=_on_dl_progress,
                                 on_item_done=_on_item_done,
                                 check_pause=_check_pause)

    if task_manager and task_id:
        task_manager.update(task_id,
                            progress=progress_base + progress_per_scan + progress_per_download,
                            dl_progress=100, message=f'应急管理部{source_label}下载完成({len(standards)}条)',
                            stats=compute_download_stats(standards), std_items=standards)

    _log.info(f"[MEM][{source}] {source_label}下载完成: {len(standards)} 条")
    return standards
