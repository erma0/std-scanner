"""scanner.gb_scan — 国家标准扫描与下载"""

import asyncio
import os
import logging
import math

from config.settings import (
    API_BASE, DETAIL_URL, OPENSTD, PAGE_SIZE, get_delay, http_client, get_output_dir,
    create_captcha_client,
)
from app.keywords import is_safety, is_aq_yj, clean_name, set_active_group
from app.dedup import get_existing_files, add_to_existing_files_cache
from app.scanner.utils import make_filename
from app.scanner.checkpoint import get_incr_checkpoint, update_incr_checkpoint
from app.scanner.download import download_with_captcha
from app.scanner.preview import launch_browser, preview_to_pdf, PLAYWRIGHT_AVAILABLE
from app.scanner.progress import save_progress
from app.scanner.download_helpers import (
    detect_download_buttons, extract_hcno_from_html, fetch_and_save_pdf,
)
from app.helpers import normalize_code

_log = logging.getLogger('std_scraper')

# 进度保存间隔（每处理 N 条保存一次）
_PROGRESS_SAVE_INTERVAL = 10


# ==================== Phase 1: API 扫描 ====================
async def scan_pages(max_results=500, incr=False, keyword_group=None, on_progress=None, check_pause=None, on_intermediate=None, state='现行'):
    """扫描国家标准 API，按 max_results 控制采集条数。
    
    网页默认按 id 降序（最新在前）。incr=True 时从 checkpoint 读取
    上次首次采集的第一条 ID（即最新的一条），如果当前页的第一条 ID
    与 checkpoint 相同，说明没有新数据，增量扫描完成。

    state: 标准状态筛选 — ''(全部), '现行', '即将实施', '废止'。默认 '现行'
    on_progress: 可选 async callable(pct, msg)，每页扫描后调用一次
    check_pause: 可选 async callable() → bool，每页前调用，返回 False 则中止扫描
    on_intermediate: 可选 async callable(standards_list)，每页扫描后推送累计结果
    """
    ckpt = get_incr_checkpoint('gb') if incr else None
    # 用 first_id 精确比对（API 内部唯一 ID，比 code 更可靠）
    last_first_id = ckpt.get('first_id') if ckpt else None
    all_standards = []

    set_active_group(keyword_group or '安全生产')

    # 从 max_results 计算所需页数（每页 PAGE_SIZE=50 条）
    max_pages = math.ceil(max_results / PAGE_SIZE)

    _log.info(f"[SCAN] 最大采集: {max_results}条 (最多{max_pages}页), 增量: {incr}"
              + (f", 上次位置: id={last_first_id or 'N/A'}" if incr else ""))

    p = 0  # 初始化，避免 max_results=0 时循环不执行导致下方引用未定义
    for p in range(1, max_pages + 1):
        if check_pause:
            if not await check_pause():
                _log.info("扫描被中止（暂停/删除）")
                break
        try:
            loop = asyncio.get_running_loop()
            data = await loop.run_in_executor(None, fetch_api_page, p, state)
        except Exception as e:
            _log.warning(f"第{p}页请求失败: {e}, 跳过")
            await asyncio.sleep(get_delay() * 2)
            continue

        rows = data.get('rows', [])
        if not rows:
            break

        # 增量短路：首页第一条 id 与上次 checkpoint 完全相同 → 无任何新数据
        if incr and last_first_id and rows[0].get('id') == last_first_id:
            _log.info("  首页首条与上次位置一致，无新增数据")
            break

        # 增量逐条比对：遍历每条记录，遇到上次扫描的"最新一条"即说明后续都已扫过
        # 这样无论新增 0 条还是 N 条，都只扫到"上次最新位置"就停（不再扫满 max_results）
        cnt = 0
        hit_checkpoint = False
        for row in rows:
            if len(all_standards) >= max_results:
                break
            # 增量命中：当前条目就是上次扫描的最新一条 → 该条及之后都已扫过
            if incr and last_first_id and row.get('id') == last_first_id:
                _log.info(f"  第{p}页命中上次采集位置，增量扫描完成")
                hit_checkpoint = True
                break

            code = normalize_code(row.get('C_STD_CODE', ''))
            name = clean_name(row.get('C_C_NAME', ''))
            if is_safety(name, std_type='gb') or is_aq_yj(code):
                all_standards.append({
                    'id': row['id'],
                    'stdCode': code,
                    'stdName': name,
                    'stdNature': row.get('STD_NATURE', ''),
                    'state': row.get('STATE', ''),
                    'issueDate': row.get('ISSUE_DATE', ''),
                    'actDate': row.get('ACT_DATE', ''),
                    'hcno': None,
                    'dlStatus': None,
                })
                cnt += 1

        _log.info(f"第{p}页: +{cnt} 条, 累计 {len(all_standards)}")

        if hit_checkpoint:
            break

        if on_progress:
            await on_progress(min(99, int(100 * p / max_pages)), f"扫描第{p}/{max_pages}页")

        if on_intermediate and all_standards:
            await on_intermediate(list(all_standards))

        if len(all_standards) >= max_results:
            break

        # 每 5 页保存 checkpoint（容错，避免崩溃丢失全部进度）
        # first_id 是增量精确比对的关键字段，必须保存
        if p % 5 == 0 and all_standards:
            update_incr_checkpoint('gb', None, {
                'first_id': all_standards[0].get('id'),
                'first_code': all_standards[0].get('stdCode'),
                'first_name': all_standards[0].get('stdName'),
                'last_page': p,
                'count': len(all_standards),
            })

        await asyncio.sleep(get_delay())

    # 最终保存 checkpoint
    if all_standards:
        update_incr_checkpoint('gb', None, {
            'first_code': all_standards[0].get('stdCode'),
            'first_name': all_standards[0].get('stdName'),
            'first_id': all_standards[0].get('id'),
            'last_page': p,
            'count': len(all_standards),
        })

    return all_standards

# ==================== Phase 2: 提取 hcno（纯 HTTP，无需浏览器）====================
async def extract_hcno(standards, on_progress=None):
    """提取 hcno：从 std.samr.gov.cn 详情页 JS 中提取。

    API 返回的 id 并非 hcno（二者是不同的标识符），必须通过
    DETAIL_URL?id={api_id} 访问详情页，从 JS 跳转逻辑中提取真正的 hcno
    （匹配 newGbInfo?hcno=xxx 模式）。

    Args:
        on_progress: 可选 async callable(pct, msg)，每处理一条调用一次
    """
    needs = [s for s in standards if not s.get('hcno')]
    if not needs:
        _log.info("[HCNO] 全部已有, 跳过")
        return

    _log.info(f"[HCNO] 从详情页提取: {len(needs)} 条...")
    done, failed, no_hcno = 0, 0, 0
    total = len(needs)
    loop = asyncio.get_running_loop()
    for i, s in enumerate(needs):
        # 在处理前立即推送进度，避免 HTTP 请求期间用户看不到反馈
        if on_progress:
            # hcno 提取占下载阶段的前 15%（0-15%）
            pct = min(15, int(15 * (i + 1) / total))
            await on_progress(pct, f"提取链接 {i + 1}/{total}")

        try:
            resp = await loop.run_in_executor(
                None, http_client.get, f"{DETAIL_URL}?id={s['id']}")
            resp.raise_for_status()
            # 匹配详情页 JS 中的 hcno 跳转：'...newGbInfo?hcno=ABC123...'
            hcno = extract_hcno_from_html(resp.text)
            if hcno:
                s['hcno'] = hcno
                done += 1
            elif "newGbInfo" in resp.text:
                # 页面有 newGbInfo 但 hcno 为空值 → 网站尚未分配，非失败
                s['dlStatus'] = 'no_hcno'
                no_hcno += 1
            else:
                s['dlStatus'] = 'failed_hcno'
                failed += 1
        except Exception as e:
            _log.debug(f"hcno 提取失败 (id={s.get('id', '?')}): {e}")
            s['dlStatus'] = 'failed_hcno'
            failed += 1

        if (i + 1) % 20 == 0:
            _log.info(f"   {done}/{i + 1} 完成")
            await loop.run_in_executor(None, save_progress, standards)

        await asyncio.sleep(get_delay())

    _log.info(f"[HCNO] 提取完成: {done} 成功, {no_hcno} 未分配, {failed} 失败")

# ==================== 按钮检测正则 ====================
# 兼容旧导入：从 download_helpers 复用（保留此处导出以避免破坏外部引用）
# _RE_XZ_BTN / _RE_CK_BTN 已迁移至 app.scanner.download_helpers

# ==================== Phase 3: 下载 PDF ====================
async def download_phase(standards, existing=None, allow_preview_override=None, on_progress=None, on_item_done=None, check_pause=None):
    """下载/预览 PDF：检测按钮 → 按类型处理

    三路判断：
    1. 有下载按钮 → download_with_captcha 直接下载
    2. 仅预览按钮（无下载按钮）→ preview_to_pdf 预览下载（受 allow_preview 开关控制）
    3. 都没有 → 不可下载（版权保护/未收录/无全文）

    按钮检测：用正则匹配 <button class="...xz_btn..."> 实际按钮元素，
    排除 JS 代码中的 .xz_btn 选择器引用。

    并发：读 config.download.concurrent（默认 1）。>1 时下载阶段并发执行，
    每个并发任务持有一个独立的 httpx.Client（避免 GB 验证码 session 串扰）。
    按钮检测仍串行（保护 samr.gov.cn 详情页 API）。

    on_progress: 可选 async callable(pct, msg)，每处理一条调用一次
    on_item_done: 可选 async callable(item_name='')，每完成一条标准处理后调用（用于实时推送 stats）
    check_pause: 可选 async callable() → bool，每项处理前调用，返回 False 则中止下载
    """
    from config.manager import load_config
    dl_cfg = load_config().get('download', {})
    strategy = dl_cfg.get('strategy', 'full')
    if allow_preview_override is not None:
        allow_preview = allow_preview_override and PLAYWRIGHT_AVAILABLE
    else:
        allow_preview = dl_cfg.get('allow_preview', True) and PLAYWRIGHT_AVAILABLE
    if strategy == 'scan_only':
        _log.info("[DL] scan_only 模式，跳过下载")
        return

    concurrent = max(1, min(10, int(dl_cfg.get('concurrent', 1))))
    if concurrent > 1:
        _log.info(f"[DL] 并发度: {concurrent}（每任务独立 client）")

    output_dir = get_output_dir()
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    if existing is None:
        existing = get_existing_files()

    _has_hcno = len([s for s in standards if s.get('hcno')])
    _log.info(f"[DL] 总匹配: {len(standards)} (已有hcno: {_has_hcno}, 需提取: {len(standards) - _has_hcno}, 已有文件: {len(existing)})")
    _total = len(standards)

    # stats 和 done_counter 由多个并发任务共享，需要锁保护
    stats = {'downloaded': 0, 'skipped_existing': 0, 'skipped_nodl': 0, 'previewed': 0, 'failed': 0}
    stats_lock = asyncio.Lock()
    done_counter = [0]  # 用 list 包裹便于闭包修改
    _detection_done = [False]  # 标记按钮检测阶段是否完成

    async def _report_done(item_name=''):
        """线程安全的进度回调（统计 + on_item_done + 周期性持久化）"""
        async with stats_lock:
            done_counter[0] += 1
            idx = done_counter[0]
            if idx % _PROGRESS_SAVE_INTERVAL == 0:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, save_progress, standards)
            if on_item_done:
                await on_item_done(item_name)
            # 检测阶段不通过 _report_done 推送 on_progress（由 _report_detect 负责）
            # 下载阶段才推送下载进度（30%-99%）
            if on_progress and _detection_done[0]:
                # 下载进度映射到 30-99% 区间
                dl_pct = 30 + int(69 * idx / _total)
                await on_progress(min(99, dl_pct), f"下载中 {idx}/{_total}")

    # ============ 串行阶段：提取hcno + 按钮检测 + 分类 ============
    # 合并原 extract_hcno 阶段：每条标准先提取 hcno（如果空），再检测按钮。
    # 这样进度条从扫描完成后持续移动，不再有独立的"提取链接"卡顿阶段。
    # 详情页（std.samr.gov.cn）和按钮检测（openstd.samr.gov.cn）是不同域名，
    # 但为保护网站，每条标准处理完仍保留一次 sleep。
    download_tasks = []  # [(s, hcno, filepath, filename), ...]
    preview_tasks = []   # 同上
    browser_ctx = None
    playwright_mgr = None

    async def _report_detect(idx):
        """串行处理阶段的进度回调（在每条标准处理前调用，立即反馈进度）"""
        if on_progress:
            # 串行阶段（提取hcno+按钮检测）占下载阶段的 0-30%
            detect_pct = min(29, int(30 * idx / _total))
            await on_progress(detect_pct, f"下载中 {idx}/{_total}")

    try:
        loop = asyncio.get_running_loop()
        for i, s in enumerate(standards):
            # 在处理每条标准前立即推送进度，避免 HTTP 请求期间用户看不到反馈
            await _report_detect(i + 1)

            if check_pause:
                if not await check_pause():
                    _log.info("下载被中止（暂停/删除）")
                    break

            # 跳过已下载/已存在的项
            dl = s.get('dlStatus')
            if dl in ('downloaded', 'skipped_existing', 'previewed'):
                await _report_done(s.get('stdName', ''))
                continue

            # 如果 hcno 为空，先从详情页提取（合并原 extract_hcno 阶段）
            if not s.get('hcno'):
                try:
                    resp = await loop.run_in_executor(
                        None, http_client.get, f"{DETAIL_URL}?id={s['id']}")
                    resp.raise_for_status()
                    hcno = extract_hcno_from_html(resp.text)
                    if hcno:
                        s['hcno'] = hcno
                    elif "newGbInfo" in resp.text:
                        # 页面有 newGbInfo 但 hcno 为空 → 网站尚未分配
                        s['dlStatus'] = 'no_hcno'
                        await _report_done(s.get('stdName', ''))
                        continue
                    else:
                        s['dlStatus'] = 'failed_hcno'
                        async with stats_lock:
                            stats['failed'] += 1
                        await _report_done(s.get('stdName', ''))
                        continue
                except Exception as e:
                    _log.debug(f"hcno 提取失败 (id={s.get('id', '?')}): {e}")
                    s['dlStatus'] = 'failed_hcno'
                    async with stats_lock:
                        stats['failed'] += 1
                    await _report_done(s.get('stdName', ''))
                    continue

            hcno = s['hcno']
            display = f"{s['stdCode'][:20]} {s['stdName'][:25]}"

            filename = make_filename(s['stdCode'], s['stdName'])
            filepath = output_dir / filename
            if filename in existing:
                s['dlStatus'] = 'skipped_existing'
                async with stats_lock:
                    stats['skipped_existing'] += 1
                await _report_done(s.get('stdName', ''))
                continue

            try:
                # 检测详情页按钮（串行：保护详情页 API）
                detail_url = f"{OPENSTD}/std/newGbInfo?hcno={hcno}"
                resp = await loop.run_in_executor(None, lambda: http_client.get(detail_url))
                resp.raise_for_status()
                html = resp.text
                btns = detect_download_buttons(html)
                can_dl = btns.can_download
                can_preview = btns.can_preview and allow_preview
                _log.debug(f"   [{i+1}] {display} html={len(html)} dl={btns.has_download} pv={btns.has_preview} cp={btns.copyright}")

                if can_dl:
                    download_tasks.append((s, hcno, filepath, filename, display))
                elif can_preview:
                    preview_tasks.append((s, hcno, filepath, filename, display))
                else:
                    if btns.copyright:
                        s['dlStatus'] = 'copyright'
                    elif btns.has_preview and not allow_preview:
                        s['dlStatus'] = 'preview_disabled'
                    else:
                        s['dlStatus'] = 'no_fulltext'
                    async with stats_lock:
                        stats['skipped_nodl'] += 1
                    await _report_done(s.get('stdName', ''))
            except Exception as e:
                _log.error(f"按钮检测异常 {s.get('stdCode')}: {e}", exc_info=True)
                s['dlStatus'] = 'failed'
                async with stats_lock:
                    stats['failed'] += 1
                await _report_done(s.get('stdName', ''))

            # 每条标准处理完保留 sleep（保护详情页 API）
            await asyncio.sleep(get_delay())

        # ============ 并发阶段：下载（独立 client 避免验证码串扰） ============
        _detection_done[0] = True
        _log.info(f"[DL] 按钮检测完成: {len(download_tasks)} 可下载, {len(preview_tasks)} 可预览")
        if on_progress:
            await on_progress(30, f"开始下载 {len(download_tasks)} 条")

        sem = asyncio.Semaphore(concurrent)

        async def _do_download(task_info):
            s, hcno, filepath, filename, display = task_info
            async with sem:
                if check_pause and not await check_pause():
                    return
                s['dlStatus'] = 'downloading'
                _log.info(f"   {display}... DOWN")
                # 并发任务在 executor 中创建独立 client 并下载
                loop = asyncio.get_running_loop()

                def _dl_with_own_client():
                    client = create_captcha_client('gb')
                    try:
                        return download_with_captcha(hcno, client=client)
                    finally:
                        try:
                            client.close()
                        except Exception:
                            pass

                pdf_data = await loop.run_in_executor(
                    None, fetch_and_save_pdf,
                    _dl_with_own_client, filepath, filename, output_dir)
                if pdf_data:
                    s['dlStatus'] = 'downloaded'
                    s['fileSize'] = len(pdf_data)
                    async with stats_lock:
                        stats['downloaded'] += 1
                    _log.info(f"[DOWN] {display} {len(pdf_data)/1024:.0f}KB")
                else:
                    s['dlStatus'] = 'failed'
                    async with stats_lock:
                        stats['failed'] += 1
                    _log.warning(f"[FAIL] {display} 下载失败")
                await _report_done(s.get('stdName', ''))

        # 预览任务保持串行（Playwright 单浏览器实例不支持并发）
        async def _do_preview(task_info):
            s, hcno, filepath, filename, display = task_info
            nonlocal browser_ctx, playwright_mgr
            if check_pause and not await check_pause():
                return
            s['dlStatus'] = 'downloading'
            _log.info(f"   {display}... PREV")
            if not browser_ctx:
                playwright_mgr, browser_ctx = await launch_browser()
            success = await preview_to_pdf(hcno, str(filepath), browser_ctx)
            if success and filepath.stat().st_size > 1000:
                s['dlStatus'] = 'previewed'
                async with stats_lock:
                    stats['previewed'] += 1
                add_to_existing_files_cache(filename)
                _log.info(f"[PREV] {display} {filepath.stat().st_size/1024:.0f}KB")
            else:
                s['dlStatus'] = 'failed_preview'
                async with stats_lock:
                    stats['failed'] += 1
                _log.warning(f"[FAIL] {display} 预览失败")
            await _report_done(s.get('stdName', ''))

        # 下载任务并发执行
        if download_tasks:
            await asyncio.gather(*[_do_download(t) for t in download_tasks])
        # 预览任务串行执行（Playwright 限制）
        for t in preview_tasks:
            await _do_preview(t)

    finally:
        # 浏览器泄漏防护：无论正常结束还是异常退出，都确保关闭 Playwright
        if playwright_mgr:
            try:
                await playwright_mgr.__aexit__(None, None, None)
            except Exception as e:
                _log.debug(f"Playwright 关闭异常: {e}")

    _log.info(f"\n[DL] 下载: {stats['downloaded']} | 预览: {stats['previewed']} | 跳过: {stats['skipped_existing'] + stats['skipped_nodl']} | 失败: {stats['failed']}")

# ==================== 国家标准 API 查询 ====================
def fetch_api_page(page, state='', query=None):
    """国家标准分页查询（同步），页面默认按 id 降序（最新在前）

    真实接口：std.samr.gov.cn/gb/search/gbQueryPage（GET 请求）
    参数：searchText, ics, state, ISSUE_DATE, sortOrder, pageSize, pageNumber
    state: 空/''(全部), 单值, 或逗号分隔多值(仅取第一个)。API 格式为 G_STATE:"值"
    响应字段：id, C_C_NAME, C_STD_CODE, STD_NATURE, ACT_DATE, STATE, ISSUE_DATE, PROJECT_ID
    GB 网站支持的状态: 现行/即将实施/废止/''(全部)。
    """
    if state and ',' in state:
        state = state.split(',')[0]
    params = {
        'pageNumber': page,
        'pageSize': PAGE_SIZE,
        'sortOrder': 'desc',
        'sortName': 'id',
    }
    if state:
        params['state'] = f'G_STATE:"{state}"'
    if query:
        params['searchText'] = query
    # 真实接口使用 GET 请求（已通过浏览器抓包确认）
    resp = http_client.get(API_BASE, params=params, headers={
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': 'https://std.samr.gov.cn/gb/gbQuery',
    })
    resp.raise_for_status()
    return resp.json()


