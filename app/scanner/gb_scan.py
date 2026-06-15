"""scanner.gb_scan — 国家标准扫描与下载"""

import asyncio
import os
import logging
import re
import math

from config.settings import (
    API_BASE, DETAIL_URL, OPENSTD, PAGE_SIZE, DELAY, http_client, get_output_dir,
)
from app.keywords import is_safety, is_aq_yj, clean_name, set_active_group
from app.dedup import get_existing_files, add_to_existing_files_cache
from app.scanner.utils import make_filename
from app.scanner.checkpoint import get_incr_checkpoint, update_incr_checkpoint
from app.scanner.download import download_with_captcha
from app.scanner.preview import launch_browser, preview_to_pdf, PLAYWRIGHT_AVAILABLE
from app.scanner.progress import save_progress
from app.helpers import normalize_code, atomic_write

_log = logging.getLogger('std_scraper')


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
    first_code_of_last_scan = ckpt.get('first_code') if ckpt else None
    all_standards = []

    set_active_group(keyword_group or '安全生产')

    # 从 max_results 计算所需页数（每页 PAGE_SIZE=50 条）
    max_pages = math.ceil(max_results / PAGE_SIZE)

    _log.info(f"[SCAN] 最大采集: {max_results}条 (最多{max_pages}页), 增量: {incr}")

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
            await asyncio.sleep(DELAY * 2)
            continue

        rows = data.get('rows', [])
        if not rows:
            break

        # 增量检测：首页第一条标准号与上次 checkpoint 相同 → 无新数据
        if incr and first_code_of_last_scan and rows[0].get('C_STD_CODE') and rows[0].get('C_STD_CODE') == first_code_of_last_scan:
            _log.info("  已到上次采集位置，增量扫描完成")
            break

        cnt = 0
        for row in rows:
            if len(all_standards) >= max_results:
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

        if on_progress:
            await on_progress(min(99, int(100 * p / max_pages)), f"扫描第{p}/{max_pages}页 ({len(all_standards)}条)")

        if on_intermediate and all_standards:
            await on_intermediate(list(all_standards))

        if len(all_standards) >= max_results:
            break

        # 每 5 页保存 checkpoint（容错）
        if p % 5 == 0 and all_standards:
            update_incr_checkpoint('gb', None, {
                'first_code': all_standards[0].get('stdCode'),
                'first_name': all_standards[0].get('stdName'),
                'last_page': p,
                'count': len(all_standards),
            })

        await asyncio.sleep(DELAY)

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
async def extract_hcno(standards):
    """提取 hcno：从 std.samr.gov.cn 详情页 JS 中提取。

    API 返回的 id 并非 hcno（二者是不同的标识符），必须通过
    DETAIL_URL?id={api_id} 访问详情页，从 JS 跳转逻辑中提取真正的 hcno
    （匹配 newGbInfo?hcno=xxx 模式）。
    """
    needs = [s for s in standards if not s.get('hcno')]
    if not needs:
        _log.info("[HCNO] 全部已有, 跳过")
        return

    _log.info(f"[HCNO] 从详情页提取: {len(needs)} 条...")
    done, failed, no_hcno = 0, 0, 0
    loop = asyncio.get_running_loop()
    for i, s in enumerate(needs):
        try:
            resp = await loop.run_in_executor(
                None, http_client.get, f"{DETAIL_URL}?id={s['id']}")
            resp.raise_for_status()
            # 匹配详情页 JS 中的 hcno 跳转：'...newGbInfo?hcno=ABC123...'
            m = re.search(r'newGbInfo\?hcno=([A-Fa-f0-9]+)', resp.text)
            if m:
                s['hcno'] = m.group(1)
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
            save_progress(standards)

        await asyncio.sleep(DELAY)

    _log.info(f"[HCNO] 提取完成: {done} 成功, {no_hcno} 未分配, {failed} 失败")

# ==================== 按钮检测正则 ====================
# 匹配 <button> 标签中的 xz_btn / ck_btn class（而非 JS 代码中的选择器）
_RE_XZ_BTN = re.compile(r'<button[^>]*class="[^"]*xz_btn[^"]*"[^>]*>')
_RE_CK_BTN = re.compile(r'<button[^>]*class="[^"]*ck_btn[^"]*"[^>]*>')

# ==================== Phase 3: 下载 PDF ====================
async def download_phase(standards, existing=None, allow_preview_override=None, on_progress=None, on_item_done=None, check_pause=None):
    """下载/预览 PDF：检测按钮 → 按类型处理

    三路判断：
    1. 有下载按钮 → download_with_captcha 直接下载
    2. 仅预览按钮（无下载按钮）→ preview_to_pdf 预览下载（受 allow_preview 开关控制）
    3. 都没有 → 不可下载（版权保护/未收录/无全文）

    按钮检测：用正则匹配 <button class="...xz_btn..."> 实际按钮元素，
    排除 JS 代码中的 .xz_btn 选择器引用。

    on_progress: 可选 async callable(pct, msg)，每处理一条调用一次
    on_item_done: 可选 async callable(item_name='')，每完成一条标准处理后调用（用于实时推送 stats）
    check_pause: 可选 async callable() → bool，每项处理前调用，返回 False 则中止下载
    """
    from config.manager import load_config
    strategy = load_config().get('download', {}).get('strategy', 'full')
    if allow_preview_override is not None:
        allow_preview = allow_preview_override and PLAYWRIGHT_AVAILABLE
    else:
        allow_preview = load_config().get('download', {}).get('allow_preview', True) and PLAYWRIGHT_AVAILABLE
    if strategy == 'scan_only':
        _log.info("[DL] scan_only 模式，跳过下载")
        return

    output_dir = get_output_dir()
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    if existing is None:
        existing = get_existing_files()
    to_process = [s for s in standards if s.get('hcno') and (not s.get('dlStatus') or s['dlStatus'] not in ('downloaded', 'skipped_existing', 'previewed'))]

    if not to_process:
        _log.info("[DL] 无需下载")
        return

    _log.info(f"[DL] 待处理: {len(to_process)} (已有文件: {len(existing)})")

    browser_ctx = None
    playwright_mgr = None

    stats = {'downloaded': 0, 'skipped_existing': 0, 'skipped_nodl': 0, 'previewed': 0, 'failed': 0}
    try:
        for i, s in enumerate(to_process):
            if check_pause:
                if not await check_pause():
                    _log.info("下载被中止（暂停/删除）")
                    break
            idx = i + 1
            filename = make_filename(s['stdCode'], s['stdName'])
            filepath = output_dir / filename

            if filename in existing:
                s['dlStatus'] = 'skipped_existing'
                stats['skipped_existing'] += 1
                if on_item_done:
                    await on_item_done(s.get('stdName', ''))
                continue

            hcno = s['hcno']
            display = f"{s['stdCode'][:20]} {s['stdName'][:25]}"

            try:
                # 检测详情页按钮（正则匹配 <button> 元素，排除 JS 选择器引用）
                detail_url = f"{OPENSTD}/std/newGbInfo?hcno={hcno}"
                loop = asyncio.get_running_loop()
                resp = await loop.run_in_executor(None, lambda: http_client.get(detail_url))
                resp.raise_for_status()
                html = resp.text
                has_download = bool(_RE_XZ_BTN.search(html))
                has_preview = bool(_RE_CK_BTN.search(html))
                copyright = '涉及版权保护' in html or '不提供在线阅读' in html or ('ISO、IEC' in html and '版权保护' in html)
                can_dl = has_download and not copyright
                can_preview = has_preview and not copyright and allow_preview
                _log.debug(f"   [{idx}] {display} html={len(html)} dl={has_download} pv={has_preview} cp={copyright}")

                if can_dl:
                    # 有下载按钮 → 直接下载
                    _log.info(f"   [{idx}/{len(to_process)}] {display}... DOWN")
                    pdf_data = await asyncio.get_running_loop().run_in_executor(
                        None, download_with_captcha, hcno)
                    if pdf_data:
                        atomic_write(str(filepath), pdf_data, dir_=str(output_dir))
                        sz = len(pdf_data) / 1024
                        s['dlStatus'] = 'downloaded'
                        s['fileSize'] = len(pdf_data)
                        stats['downloaded'] += 1
                        add_to_existing_files_cache(filename)
                        _log.info(f"[DOWN] {sz:.0f}KB")
                    else:
                        s['dlStatus'] = 'failed'
                        stats['failed'] += 1
                        _log.warning("[FAIL] 下载失败")

                elif can_preview:
                    # 仅预览按钮（无下载按钮）→ 预览下载
                    _log.info(f"   [{idx}/{len(to_process)}] {display}... PREV")
                    if not browser_ctx:
                        playwright_mgr, browser_ctx = await launch_browser()
                    success = await preview_to_pdf(hcno, str(filepath), browser_ctx)
                    if success and filepath.stat().st_size > 1000:
                        sz = filepath.stat().st_size / 1024
                        s['dlStatus'] = 'previewed'
                        stats['previewed'] += 1
                        add_to_existing_files_cache(filename)
                        _log.info(f"[PREV] {sz:.0f}KB")
                    else:
                        s['dlStatus'] = 'failed_preview'
                        stats['failed'] += 1
                        _log.warning("[FAIL] 预览失败")

                else:
                    if copyright:
                        s['dlStatus'] = 'copyright'
                    elif has_preview and not allow_preview:
                        s['dlStatus'] = 'preview_disabled'
                    else:
                        s['dlStatus'] = 'no_fulltext'
                    stats['skipped_nodl'] += 1

                if on_item_done:
                    await on_item_done(s.get('stdName', ''))

            except Exception:
                s['dlStatus'] = 'failed'
                stats['failed'] += 1
                if on_item_done:
                    await on_item_done(s.get('stdName', ''))

            if idx % 10 == 0:
                save_progress(standards)

            await asyncio.sleep(DELAY)

            if on_progress:
                await on_progress(min(99, int(100 * idx / len(to_process))), f"处理中 {idx}/{len(to_process)}")
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


