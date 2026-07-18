"""scanner.hb_scan — 行业标准扫描与下载"""

import asyncio
import os
import time
import logging
import math
from urllib.parse import urlencode

from config.settings import (
    HB_API_URL, get_delay, http_client, HB_CODE_MAP, HB_SAFETY_CODES,
    _resolve_hb_industry, get_output_dir, get_captcha_client,
)
from app.keywords import is_safety, is_aq_yj, set_active_group
from app.dedup import get_existing_files
from app.scanner.utils import make_filename
from app.scanner.checkpoint import get_incr_checkpoint, update_incr_checkpoint
from app.scanner.download_helpers import fetch_and_save_pdf
from app.helpers import normalize_code

_log = logging.getLogger('std_scraper')


class CopyrightError(Exception):
    """标准因版权/政策原因不公开全文，不可下载也不可重试。

    触发条件：/portal/online/{pk} 页面返回"尚未公开"提示。
    调用方应捕获此异常并将 dlStatus 标记为 'copyright'（不可重试）。
    """
    pass


def fetch_hb_list(industry='', key='', status=None, page=1, size=100):
    """调用行业标准列表 API

    status: 字符串(单值)或 None(默认现行)。
    网站仅支持 '现行'/'即将实施'/'废止'/''(全部)，其他值回退到 '现行'。
    """
    # 网站实际支持的状态值白名单
    _VALID_STATUS = {'现行', '即将实施', '废止', ''}
    if status:
        statuses = status if isinstance(status, list) else [status]
        # 过滤掉网站不支持的状态值，回退到 '现行'
        statuses = [s for s in statuses if s in _VALID_STATUS] or ['现行']
    else:
        statuses = ['现行']
    params = {
        'current': page, 'size': size, 'key': key,
        'ministry': '', 'industry': industry,
        'pubdate': '', 'date': '',
    }
    encoded = urlencode(params) + '&' + '&'.join(
        f"status[]={urlencode({'v': s}).split('=', 1)[1]}" for s in statuses
    )
    resp = http_client.post(HB_API_URL, content=encoded.encode(), headers={
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Referer': 'https://hbba.sacinfo.org.cn/stdList',
        'Content-Type': 'application/x-www-form-urlencoded',
        'X-Requested-With': 'XMLHttpRequest',
        'Origin': 'https://hbba.sacinfo.org.cn',
    })
    resp.raise_for_status()
    return resp.json()


def _scan_list_standards(items, item_label, fetch_fn, site_type, log_prefix, max_results=500, incr=False, on_progress=None, on_intermediate=None, check_pause=None):
    """统一的行业/地方标准扫描，支持 max_results + incr。

    网页默认按最新在前排序。incr=True 时从 checkpoint 读取各 item
    上次扫描的第一条 pk，如果当前页第一条 pk 相同则说明无新数据。

    max_results 为每个行业/省份的独立配额，互不影响。
    例如 max_results=500 + 16个行业 = 每个行业最多500条，总计理论上限8000。

    on_progress: 可选 callable(pct, msg)，每扫描一页调用一次（同步）
    on_intermediate: 可选 callable(standards)，每扫描一页后调用（同步），用于推送中间结果
    check_pause: 可选 callable() → bool，每页前调用，返回 False 则中止扫描（同步，线程内调用）
    """
    all_standards = []
    total_items = len(items)
    for item_idx, item in enumerate(items):
        label = item or '全部'
        _log.info(f"{log_prefix} {item_label}: {label}")

        item_ckpt = get_incr_checkpoint(site_type, item) if incr else None
        # 用 first_pk 精确比对（pk 是 sha256 主键，唯一可靠）
        last_first_pk = item_ckpt.get('first_pk') if item_ckpt else None
        if incr and last_first_pk:
            _log.info(f"   增量模式: 上次位置 pk={last_first_pk}")

        item_count = 0
        max_pages = math.ceil(max_results / 100)
        found_checkpoint = False

        for page in range(1, max_pages + 1):
            if check_pause:
                if not check_pause():
                    _log.info(f"   {item_label}={label} 扫描被中止")
                    break
            try:
                result = fetch_fn(item, page, size=100)
            except Exception as e:
                _log.warning(f"   {item_label}={label} 第{page}页请求失败: {e}")
                break

            records = result.get('records', [])
            if not records:
                if page == 1:
                    _log.info(f"   {item_label}={label} 该状态无数据，跳过")
                break

            # 增量短路：首页第一条 pk 与上次 checkpoint 完全相同 → 无任何新数据
            if incr and last_first_pk and records[0].get('pk') == last_first_pk:
                _log.info(f"   {item_label}={label} 首页首条与上次位置一致，无新增数据")
                found_checkpoint = True
                break

            # 增量逐条比对：遍历每条记录，遇到上次扫描的"最新一条"即说明后续都已扫过
            # 这样无论新增 0 条还是 N 条，都只扫到"上次最新位置"就停（不再扫满 max_results）
            cnt = 0
            hit_checkpoint = False
            for r in records:
                if item_count >= max_results:
                    break
                # 增量命中：当前条目就是上次扫描的最新一条 → 该条及之后都已扫过
                if incr and last_first_pk and r.get('pk') == last_first_pk:
                    _log.info(f"   {item_label}={label} 第{page}页命中上次采集位置，增量扫描完成")
                    hit_checkpoint = True
                    break

                name = r.get('chName', '')
                code = normalize_code(r.get('code', ''))
                if is_safety(name, std_type=site_type) or is_aq_yj(code):
                    all_standards.append({
                        'stdCode': code, 'stdName': name,
                        'industry': r.get('industry', ''),
                        'pk': r.get('pk', ''),
                        'state': r.get('status', ''),  # 统一为 state（对应 GB）
                        'stdNature': '',
                        'issueDate': _ms_to_date(r.get('issueDate')),
                        'actDate': _ms_to_date(r.get('actDate')),
                        'siteType': site_type,
                        'dlStatus': None,
                    })
                    cnt += 1
                    item_count += 1

            if hit_checkpoint:
                found_checkpoint = True
                break

            total = result.get('total', 0)
            pages = result.get('pages', 0)
            _log.info(f"   {item_label}={label} 第{page}/{pages}页: +{cnt}条 本项{item_count} 累计{len(all_standards)} (总{total})")

            if item_count >= max_results:
                _log.info(f"   {item_label}={label} 已达 max_results({max_results})，停止")
                break

            if on_progress:
                overall = (item_idx * max_pages + page)
                total_pages = total_items * max_pages
                pct = min(99, int(100 * overall / total_pages))
                # 与国标统一格式：显示当前页/总页数
                on_progress(pct, f"扫描{label}({item_idx + 1}/{total_items}) 第{page}/{pages}页")

            if on_intermediate:
                on_intermediate(all_standards)

            if page >= pages:
                break
            time.sleep(get_delay())

        # item='' 为留空模式（全部数据），不过滤 industry；否则按行业/省份分组
        if item == '':
            item_results = [s for s in all_standards if s.get('siteType') == site_type]
        else:
            item_results = [s for s in all_standards
                            if s.get('siteType') == site_type and s.get('industry') == item]
        if item_results:
            ckpt_page = page - 1 if found_checkpoint else page
            if ckpt_page > 0:
                update_incr_checkpoint(site_type, item, {
                    'first_pk': item_results[0].get('pk'),
                    'first_code': item_results[0].get('stdCode'),
                    'first_name': item_results[0].get('stdName'),
                    'page': ckpt_page,
                    'count': len(item_results),
                })

    result_label = '行业标准' if site_type == 'hb' else '地方标准'
    _log.info(f"{log_prefix} 共筛选 {len(all_standards)} 条{result_label}")
    return all_standards


async def _download_standards(standards, log_prefix='HB-DL', on_progress=None, on_item_done=None, check_pause=None):
    """统一下载逻辑（行业/地方标准共用）

    并发：读 config.download.concurrent（默认 1）。>1 时下载并发执行，
    每个并发任务持有一个独立的 httpx.Client（避免 cookie 串扰）。

    on_progress: 可选 async callable(pct, msg)，每下载一条调用一次
    on_item_done: 可选 async callable(item_name='')，每完成一条标准处理后调用（用于实时推送 stats）
    check_pause: 可选 async callable() → bool，每项处理前调用，返回 False 则中止下载
    """
    from config.manager import load_config
    from config.settings import create_captcha_client

    output_dir = get_output_dir()
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    existing = get_existing_files()
    to_download = [s for s in standards if s.get('pk')]

    concurrent = max(1, min(10, int(load_config().get('download', {}).get('concurrent', 1))))
    if concurrent > 1:
        _log.info(f"{log_prefix} 并发度: {concurrent}（每任务独立 client）")

    # stats 和 done_counter 由多个并发任务共享，需要锁保护
    stats = {'downloaded': 0, 'skipped_existing': 0, 'failed': 0, 'copyright': 0}
    stats_lock = asyncio.Lock()
    done_counter = [0]
    total = len(to_download)

    async def _report_done(item_name=''):
        async with stats_lock:
            done_counter[0] += 1
            idx = done_counter[0]
            if on_item_done:
                await on_item_done(item_name)
            if on_progress and total > 0:
                await on_progress(int(100 * idx / total), f"下载中 {idx}/{total}")

    sem = asyncio.Semaphore(concurrent)

    async def _do_one(s):
        async with sem:
            if check_pause and not await check_pause():
                return

            filename = make_filename(s['stdCode'], s['stdName'])
            filepath = output_dir / filename

            if filename in existing:
                async with stats_lock:
                    stats['skipped_existing'] += 1
                s['dlStatus'] = 'skipped_existing'
                await _report_done(s.get('stdName', ''))
                return

            display = f"{s['stdCode'][:20]} {s['stdName'][:25]}"
            s['dlStatus'] = 'downloading'
            _log.info(f"   {display}...")

            try:
                # 并发任务在 executor 中创建独立 client
                loop = asyncio.get_running_loop()

                def _dl_with_own_client():
                    client = create_captcha_client(s['siteType'])
                    try:
                        return download_hb_with_captcha(s['pk'], s['siteType'], client=client)
                    finally:
                        try:
                            client.close()
                        except Exception:
                            pass

                pdf_data = await loop.run_in_executor(
                    None, fetch_and_save_pdf,
                    _dl_with_own_client, filepath, filename, output_dir,
                )
                if pdf_data:
                    async with stats_lock:
                        stats['downloaded'] += 1
                    s['dlStatus'] = 'downloaded'
                    _log.info(f"   [OK] {display} ({len(pdf_data)/1024:.0f}KB)")
                else:
                    async with stats_lock:
                        stats['failed'] += 1
                    s['dlStatus'] = 'failed'
                    _log.warning(f"   [FAIL] {display}")
            except CopyrightError as e:
                async with stats_lock:
                    stats['copyright'] += 1
                s['dlStatus'] = 'copyright'
                _log.info(f"   [COPY] {display}: {e}")
            except Exception as e:
                async with stats_lock:
                    stats['failed'] += 1
                s['dlStatus'] = 'failed'
                _log.error(f"   [ERR] {display}: {e}")

            await _report_done(s.get('stdName', ''))

    # 并发执行所有下载任务
    if to_download:
        await asyncio.gather(*[_do_one(s) for s in to_download])

    _log.info(f"{log_prefix} 下载:{stats['downloaded']} 跳过:{stats['skipped_existing']} 版权:{stats['copyright']} 失败:{stats['failed']}")


def scan_hb_standards(industries=None, max_results=500, incr=False, keyword_group=None, on_progress=None, on_intermediate=None, check_pause=None, status='现行'):
    """行业标准扫描（支持 max_results + 增量）

    status: 标准状态筛选 — ''(全部), '现行', '即将实施', '废止'。默认 '现行'

    on_progress: 可选 callable(pct, msg)，每扫描一页调用一次（同步）
    on_intermediate: 可选 callable(standards)，每扫描一页后调用（同步），用于推送中间结果
    check_pause: 可选 callable() → bool，每页前调用（同步）
    """
    set_active_group(keyword_group or '安全生产')

    if industries is None:
        industries = [HB_CODE_MAP[c] for c in HB_SAFETY_CODES]
    elif isinstance(industries, str):
        industries = [industries]
    if industries:
        industries = [_resolve_hb_industry(i) for i in industries]
        industries = [i for i in industries if i]
    # 空列表 → 留空模式：API 不限制行业，返回全部混合数据
    if not industries:
        industries = ['']

    def _fetch(item, page, size):
        return fetch_hb_list(industry=item, page=page, size=size, status=status)

    return _scan_list_standards(industries, '行业', _fetch, 'hb', 'HB-SCAN', max_results, incr, on_progress=on_progress, on_intermediate=on_intermediate, check_pause=check_pause)


async def download_hb_standards(standards, on_progress=None, on_item_done=None, check_pause=None):
    """行业标准下载

    on_progress: 可选 async callable(pct, msg)
    on_item_done: 可选 async callable(item_name='')，每完成一条标准处理后调用
    check_pause: 可选 async callable() → bool
    """
    await _download_standards(standards, 'HB-DL', on_progress=on_progress, on_item_done=on_item_done, check_pause=check_pause)


def download_hb_with_captcha(hb_hash, site_type='hb', client=None):
    """下载行业标准/地方标准 PDF

    实测（2026-07）：HB/DB 下载无需验证码，直接 GET /portal/download/{pk} 即可返回 PDF。
    之前误用 GB 的验证码流程（/portal/validate-code + /portal/validate-captcha/down
    + /portal/download/{download_code}），其中 download URL 用了验证码返回的 code
    而非 pk，服务器返回 302 重定向到首页，导致 HB/DB 全部下载失败。

    保留函数名（含 captcha）以保持向后兼容，但实际不走验证码流程。

    流程：
    1. GET /stdDetail/{pk}      建立 session（下发 cookie）
    2. GET /portal/download/{pk} 直接下载 PDF（用 pk，不是 download_code）
    3. 若下载返回空 → 访问 /portal/online/{pk} 检测是否"尚未公开"
       - 是 → 抛 CopyrightError（不可重试，调用方标记 dlStatus='copyright'）
       - 否 → 当作普通失败重试

    实测各行业下载可用性（2026-07）：
      AQ/XF/JG/DL/SL → 可下载
      YS/HG/JT       → 版权限制（"本系统尚未公开"）

    Args:
        hb_hash: 标准 pk（sha256 哈希）
        site_type: 'hb' | 'db'
        client: 可选，外部传入的独立 httpx.Client（并发场景使用，调用方负责关闭）
    """
    if site_type == 'hb':
        base_url = 'https://hbba.sacinfo.org.cn'
    else:
        base_url = 'https://dbba.sacinfo.org.cn'

    # client=None → 用共享 client（向后兼容）；并发场景由调用方传入独立 client
    own_client = client is not None
    if client is None:
        client = get_captcha_client(site_type)

    detail_url = f'{base_url}/stdDetail/{hb_hash}'
    list_referer = {'Referer': f'{base_url}/stdList'}
    detail_referer = {'Referer': detail_url}

    max_retries = 3
    try:
        for attempt in range(1, max_retries + 1):
            try:
                # 1. 先访问详情页建立 session（设置必要的 cookie）
                try:
                    client.get(detail_url, headers=list_referer)
                except Exception as e:
                    _log.debug(f"[HB-DL] session 预热失败 ({site_type}): {e}")

                # 2. 直接下载 PDF（用 pk，不是 download_code）
                download_url = f'{base_url}/portal/download/{hb_hash}'
                resp = client.get(download_url, headers=detail_referer)
                resp.raise_for_status()

                ct = resp.headers.get('content-type', '')
                if 'pdf' in ct.lower() or resp.content[:5] == b'%PDF-':
                    if len(resp.content) > 500:
                        return resp.content
                    _log.info(f"[HB-DL] 下载内容过小 (len={len(resp.content)}, pk={hb_hash[:16]}...)")
                else:
                    # 下载返回空或非 PDF → 检测是否版权限制
                    if len(resp.content) == 0 or ('text/html' in ct.lower()):
                        if _is_copyright_restricted(client, base_url, hb_hash, detail_url):
                            raise CopyrightError(
                                f"标准 {hb_hash[:16]}... 因版权/政策原因不公开全文"
                            )
                    body_preview = resp.content[:120]
                    _log.info(f"[HB-DL] 下载返回非PDF (ct={ct}, len={len(resp.content)}, "
                             f"body={body_preview!r}, pk={hb_hash[:16]}...)")

                # 下载失败，清空 cookie 重试
                client.cookies.clear()
                time.sleep(get_delay())
            except CopyrightError:
                # 版权限制：不重试，直接向上抛出
                raise
            except Exception as e:
                _log.info(f"[HB-DL] 下载异常 (尝试 {attempt}/{max_retries}, pk={hb_hash[:16]}...): {e}")
                time.sleep(get_delay())

        _log.warning(f"[HB-DL] 下载失败：重试耗尽 ({max_retries}/{max_retries}, pk={hb_hash[:16]}...)")
        return None
    finally:
        # 仅关闭外部传入的独立 client；共享 client 由 close_captcha_clients 统一管理
        if own_client:
            try:
                client.close()
            except Exception:
                pass


def _is_copyright_restricted(client, base_url, pk, detail_url):
    """检测标准是否因版权原因不公开全文。

    访问 /portal/online/{pk}，若页面包含"尚未公开"/"不公开"提示则返回 True。
    """
    try:
        online_url = f'{base_url}/portal/online/{pk}'
        resp = client.get(online_url, headers={'Referer': detail_url}, timeout=10)
        html = resp.text
        # HB 版权限制页面特征文本
        if '尚未公开' in html or '不公开理由' in html:
            _log.info(f"[HB-DL] 标准因版权原因不公开 (pk={pk[:16]}...)")
            return True
    except Exception as e:
        _log.debug(f"[HB-DL] 版权检测失败 (pk={pk[:16]}...): {e}")
    return False


def _ms_to_date(ms):
    """毫秒时间戳 → 日期字符串"""
    if not ms:
        return ''
    try:
        return time.strftime('%Y-%m-%d', time.localtime(ms / 1000))
    except Exception as e:
        _log.debug(f"ms_to_date 转换失败 (ms={ms}): {e}")
        return ''
