"""scanner.tt_scan — 团体标准扫描与下载

全国团体标准信息平台 https://www.ttbz.org.cn

关键发现（2026-07-19 通过 chrome-devtools MCP 抓包确认）：
  从 /standard.html 入口访问 /cms-proxy/ms/ 接口完全无 WAF 拦截，
  可用 httpx 直接调用，无需 Playwright/cookie/滑块。

接口契约：
  1. 列表搜索 POST /cms-proxy/ms/portal/standardInfo/getPortalStandardList
     Content-Type: application/x-www-form-urlencoded
     body: pageNo=1&pageSize=10&searchKey=消防
     返回: {result:true, data:{total, rows:[{standardUniqueId, standardNo,
            standardTitleCn, organName, standardStatusName, filePublishDate, ...}]}}
  2. 详情 POST /cms-proxy/ms/portal/standardInfo/getPortalStandardById
     body: standardUniqueId=xxx
     返回: {data:{files:[{fileId, fileType, fileTypeName, fileUrl, ...}]}}
  3. PDF 下载 GET {fileUrl}  （fileUrl 形如 /UploadFiles/StandardFpdFile/xxx.pdf）
     返回 application/pdf

字段映射（ttbz API → 项目内部统一字段）：
  standardUniqueId → detail_id / pid
  standardNo       → code
  standardTitleCn  → name
  organName        → group_name
  standardStatusName → status
  filePublishDate  → publishDate

增量扫描：用 standardUniqueId 作为比对字段（与 GB 的 first_id / HB-DB 的 first_pk 同机制）。
"""

import asyncio
import logging
import urllib.parse
from typing import Callable, Optional

from app.helpers import normalize_code
from app.keywords import is_safety, set_active_group
from app.scanner.checkpoint import get_incr_checkpoint, update_incr_checkpoint
from app.scanner.utils import make_filename
from config.settings import (
    TT_API_LIST, TT_API_DETAIL, TT_BASE, TT_LIST_URL,
    get_output_dir, get_delay, http_client,
)

_log = logging.getLogger('std_scraper')

# ==================== 常量 ====================
_TT_PAGE_SIZE = 50                # 列表 API 每页条数（实测可到 100，保守用 50）
_TT_DL_STATUS = 'dlStatus'        # std_items 里的下载状态字段（与 GB/HB/DB 一致）
_TT_REFERER = TT_LIST_URL         # Referer 用无 WAF 入口页
_TT_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
          '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')


def _tt_headers() -> dict:
    """构造 ttbz API 请求头。"""
    return {
        'User-Agent': _TT_UA,
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'zh-CN,zh;q=0.9',
        'Content-Type': 'application/x-www-form-urlencoded',
        'Referer': _TT_REFERER,
        'Origin': TT_BASE,
    }


def _tt_download_headers() -> dict:
    """构造 PDF 下载请求头。"""
    return {
        'User-Agent': _TT_UA,
        'Accept': 'application/pdf,*/*',
        'Referer': _TT_REFERER,
    }


def _call_list_api(page_no: int, page_size: int, search_key: str = '') -> dict:
    """同步调用列表搜索 API。返回原始 JSON dict。"""
    body = urllib.parse.urlencode({
        'pageNo': page_no,
        'pageSize': page_size,
        'searchKey': search_key,
    })
    resp = http_client.post(TT_API_LIST, content=body.encode('utf-8'),
                            headers=_tt_headers(), timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not data.get('result'):
        raise RuntimeError(f"ttbz 列表 API 返回失败: {data}")
    return data


def _call_detail_api(standard_unique_id: str) -> dict:
    """同步调用详情 API。返回原始 JSON dict。"""
    body = urllib.parse.urlencode({'standardUniqueId': standard_unique_id})
    resp = http_client.post(TT_API_DETAIL, content=body.encode('utf-8'),
                            headers=_tt_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


def _parse_list_rows(rows: list) -> list:
    """把 API 返回的 rows 转为项目内部标准条目格式。"""
    items = []
    for r in rows:
        uid = r.get('standardUniqueId') or ''
        if not uid:
            continue
        code = normalize_code(r.get('standardNo') or '')
        name = r.get('standardTitleCn') or ''
        items.append({
            'detail_id': uid,                 # standardUniqueId（API 唯一 ID）
            'pid': uid,                       # 去重 key
            'group_name': r.get('organName') or '',   # 团体名称
            'code': code,
            'name': name,
            'publishDate': r.get('filePublishDate') or '',
            'status': r.get('standardStatusName') or '现行',
            'sellable': False,                # API 未提供付费信息，统一按可下载处理
            'tid': 'BV_TT',
            'type': 'tt',
        })
    return items


# ==================== 扫描 ====================
async def scan_tt_standards(
    cnl1_codes=None,
    max_results=500,
    incr=False,
    keyword_group=None,
    on_progress: Callable = None,
    on_intermediate: Callable = None,
    check_pause: Callable = None,
    status: str = '现行',
) -> list:
    """扫描团体标准列表（httpx 直接调用 /cms-proxy/ms/ 接口，无 WAF）。

    Args:
        cnl1_codes: 中国标准分类号一级代码列表（保留参数，API 暂未使用，留作扩展）
        max_results: 最大扫描条数
        incr: 增量扫描（用 standardUniqueId 比对，命中上次位置即停）
        keyword_group: 关键词组名（'__all__'=不过滤）
        on_progress: async callable(pct, msg)
        on_intermediate: async callable(standards_list)
        check_pause: async callable() → bool
        status: 状态筛选（现行/即将实施/废止/全部）

    Returns:
        标准列表
    """
    set_active_group(keyword_group or '安全生产')
    is_all = keyword_group == '__all__'

    all_standards = []
    total_scanned = 0
    ckpt_key = 'tt_all'

    # 增量 checkpoint
    last_first_id = None
    if incr:
        ckpt = get_incr_checkpoint('tt', ckpt_key)
        if ckpt:
            last_first_id = ckpt.get('first_id')
            _log.info(f"[TT] 增量扫描，上次最新 standardUniqueId={last_first_id}")

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
            pct = min(95, int(total_scanned / max(max_results, 1) * 95))
            try:
                await on_progress(pct, f'扫描中 第{page_no}页（已{total_scanned}条）')
            except Exception:
                pass

        # 同步 HTTP 调用包装到 executor
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(
                None, _call_list_api, page_no, _TT_PAGE_SIZE, '',
            )
        except Exception as e:
            _log.error(f"[TT] 第{page_no}页列表请求失败: {e}")
            break

        rows = (data.get('data') or {}).get('rows') or []
        if not rows:
            _log.info(f"[TT] 第{page_no}页无数据，结束")
            break

        page_items = _parse_list_rows(rows)

        # 增量比对：逐条检查是否命中上次位置
        for it in page_items:
            if last_first_id and it['detail_id'] == last_first_id:
                _log.info(f"[TT] 增量命中上次位置 standardUniqueId={last_first_id}，停止")
                hit_last = True
                break
            # 状态过滤
            if status and status != '全部' and it.get('status') != status:
                continue
            # 关键词过滤（__all__ 模式跳过）
            if not is_all and not is_safety(it.get('name', ''), 'tt'):
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
        if len(rows) < _TT_PAGE_SIZE:
            break

        page_no += 1
        # 保护网站
        await asyncio.sleep(get_delay())

    # 更新 checkpoint（保存本次最新一条 standardUniqueId）
    if all_standards:
        update_incr_checkpoint('tt', ckpt_key, {
            'first_id': all_standards[0]['detail_id'],
            'first_code': all_standards[0].get('code', ''),
            'first_name': all_standards[0].get('name', ''),
            'count': len(all_standards),
        })

    _log.info(f"[TT] 扫描完成: {len(all_standards)} 条")
    return all_standards


# ==================== 搜索 ====================
async def search_tt_standards(query: str, max_results: int = 20) -> list:
    """在 ttbz.org.cn 搜索团体标准（httpx 直接调用 API，无 WAF）。

    供 /api/search/query 和 /api/search/batch 调用。
    """
    loop = asyncio.get_running_loop()
    try:
        # 搜索场景：单页拿足够多的结果（保守取 max_results * 2，上限 100）
        page_size = min(100, max(max_results * 2, 20))
        data = await loop.run_in_executor(
            None, _call_list_api, 1, page_size, query,
        )
    except Exception as e:
        _log.error(f"[TT] 搜索 '{query}' 失败: {e}")
        return []

    rows = (data.get('data') or {}).get('rows') or []
    items = _parse_list_rows(rows)
    return items[:max_results]


# ==================== 下载 ====================
def _fetch_pdf_url(standard_unique_id: str) -> Optional[str]:
    """调用详情 API 获取 PDF 文件 URL。返回 fileUrl 或 None。"""
    try:
        data = _call_detail_api(standard_unique_id)
    except Exception as e:
        _log.error(f"[TT] 详情 API 失败 ({standard_unique_id}): {e}")
        return None

    files = (data.get('data') or {}).get('files') or []
    for f in files:
        # 优先选标准文本（fileType=1 通常表示 PDF 标准文本）
        url = f.get('fileUrl')
        if url and url.lower().endswith('.pdf'):
            return url
    # 退而求其次：取第一个有 fileUrl 的文件
    for f in files:
        url = f.get('fileUrl')
        if url:
            return url
    return None


async def _download_one_standard(std: dict, existing: set) -> dict:
    """下载单个团体标准 PDF。返回更新后的 std（含 dlStatus）。

    用 httpx 直接调用：详情 API 拿 fileUrl → GET 下载 PDF。
    """
    detail_id = std.get('detail_id') or std.get('pid')
    code = std.get('code', '')
    name = std.get('name', '')

    filename = make_filename(code, name)
    if filename in existing:
        std[_TT_DL_STATUS] = 'skipped_existing'
        return std

    # 付费购买的标准不下载（API 未提供付费标识，保留兼容）
    if std.get('sellable'):
        _log.info(f"  [TT][SKIP] 付费标准: {code} {name}")
        std[_TT_DL_STATUS] = 'copyright'
        return std

    # 调用详情 API 拿 PDF URL
    loop = asyncio.get_running_loop()
    file_url = await loop.run_in_executor(None, _fetch_pdf_url, detail_id)
    if not file_url:
        _log.info(f"  [TT][SKIP] 详情无 PDF 链接: {code} {name}")
        std[_TT_DL_STATUS] = 'no_fulltext'
        return std

    # 拼接完整 URL
    if file_url.startswith('/'):
        full_url = f"{TT_BASE}{file_url}"
    else:
        full_url = file_url

    # 下载 PDF
    try:
        resp = await loop.run_in_executor(
            None,
            lambda: http_client.get(full_url, headers=_tt_download_headers(), timeout=60),
        )
        content_type = resp.headers.get('content-type', '')
        body = resp.content
        is_pdf = 'pdf' in content_type.lower() or body[:5] == b'%PDF-'
        if not is_pdf or len(body) < 1000:
            _log.info(f"  [TT][FAIL] 响应非 PDF ({content_type}, {len(body)}B): {code}")
            std[_TT_DL_STATUS] = 'failed'
            return std
        # 落盘
        out_dir = get_output_dir()
        filepath = out_dir / filename
        filepath.write_bytes(body)
        _log.info(f"  [TT][OK] {filepath} ({len(body)/1024:.0f}KB)")
        std[_TT_DL_STATUS] = 'downloaded'
        existing.add(filename)
        return std
    except Exception as e:
        _log.error(f"  [TT][ERROR] 下载失败 {code}: {e}")
        std[_TT_DL_STATUS] = 'failed'
        return std


async def download_tt_standards(
    standards: list,
    on_progress: Callable = None,
    on_item_done: Callable = None,
    check_pause: Callable = None,
):
    """下载团体标准列表（httpx 直接调用，无 Playwright）。

    并发：读 config.download.concurrent（默认 1）。>1 时下载并发执行。
    TT 为纯 PDF 下载，无 cookie session 依赖，使用共享 http_client 即可。
    """
    from app.dedup import get_existing_files
    from config.manager import load_config
    existing = get_existing_files()
    total = len(standards)

    concurrent = max(1, min(10, int(load_config().get('download', {}).get('concurrent', 1))))
    if concurrent > 1:
        _log.info(f"[TT-DL] 并发度: {concurrent}")

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
            _log.info(f"[TT] 处理: {code} {name}")

            await _download_one_standard(std, existing)

            await _report_done(f"{code} {name[:20]}")

            # 成功下载后不 sleep（与 HB/DB 一致），失败后 sleep
            if std.get(_TT_DL_STATUS) == 'failed':
                await asyncio.sleep(get_delay())

    if standards:
        await asyncio.gather(*[_do_one(s) for s in standards])


# ==================== 统一管线 ====================
async def run_tt_pipeline(
    config: dict,
    task_id: str = None,
    task_manager=None,
    progress_base: int = 0,
    progress_per_scan: int = 40,
    progress_per_download: int = 60,
) -> list:
    """团体标准统一管线：scan + download，全部基于 httpx，无需 Playwright。"""
    from app.scanner.change_tracker import compare_snapshot
    from app.scanner.utils import compute_download_stats

    max_results = config.get('max_results', 500)
    incr = config.get('incr', False)
    keyword_group = config.get('keyword_group', '安全生产')
    scan_only = config.get('scan_only', False)
    std_state = config.get('std_state', '现行')
    cnl1_codes = config.get('cnl1_codes')  # 保留参数，API 暂未使用

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
    standards_holder = []  # 闭包内可写

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
                            scan_progress=0, message='扫描团体标准...')

    standards = await scan_tt_standards(
        cnl1_codes=cnl1_codes, max_results=max_results, incr=incr,
        keyword_group=keyword_group, on_progress=_on_scan_progress,
        on_intermediate=_on_intermediate, check_pause=_check_pause, status=std_state,
    )
    standards_holder.append(standards)

    if task_manager and task_id:
        task_manager.update(task_id, progress=progress_base + progress_per_scan,
                            scan_progress=100, message=f'团体标准扫描完成({len(standards)}条)',
                            stats={'scanned': len(standards)}, std_items=standards)

    _log.info(f"[TT] 扫描完成: {len(standards)} 条")

    # 变更追踪
    try:
        changes = compare_snapshot('tt', standards)
        if task_manager and task_id and (changes.get('added') or changes.get('changed') or changes.get('removed')):
            task_manager.update(task_id, changes=changes)
    except Exception as e:
        _log.debug(f"[TT] 变更追踪异常: {e}")

    # 扫描结果为空：明确标识"无符合条件标准"，跳过下载阶段
    if not standards:
        if task_manager and task_id:
            state_label = std_state or '全部'
            task_manager.update(task_id,
                progress=progress_base + progress_per_scan + progress_per_download,
                dl_progress=100,
                message=f"无符合条件标准（{state_label}），跳过下载",
                stats={'scanned': 0, 'downloaded': 0, 'success': 0, 'failed': 0, 'skipped': 0},
                std_items=[])
        _log.info(f"[TT] 无符合条件标准（{std_state or '全部'}），跳过下载")
        return standards

    if scan_only:
        return standards

    # === 阶段2: 下载 ===
    if task_manager and task_id:
        task_manager.update(task_id, progress=progress_base + progress_per_scan + 1,
                            dl_progress=0, message='下载团体标准...')

    await download_tt_standards(standards,
                                on_progress=_on_dl_progress,
                                on_item_done=_on_item_done,
                                check_pause=_check_pause)

    if task_manager and task_id:
        task_manager.update(task_id,
                            progress=progress_base + progress_per_scan + progress_per_download,
                            dl_progress=100, message=f'团体标准下载完成({len(standards)}条)',
                            stats=compute_download_stats(standards), std_items=standards)

    _log.info(f"[TT] 下载完成: {len(standards)} 条")
    return standards
