"""scanner.hb_scan — 行业标准扫描与下载"""

import asyncio
import os
import time
import logging
import math
from urllib.parse import urlencode

from config.settings import (
    HB_API_URL, DELAY, http_client, HB_CODE_MAP, HB_SAFETY_CODES,
    _resolve_hb_industry, get_output_dir,
)
from app.keywords import is_safety, is_aq_yj, set_active_group
from app.dedup import get_existing_files, add_to_existing_files_cache
from app.scanner.utils import make_filename
from app.scanner.checkpoint import get_incr_checkpoint, update_incr_checkpoint
from app.scanner.download import _unified_captcha_download
from app.helpers import normalize_code, atomic_write

_log = logging.getLogger('std_scraper')


def fetch_hb_list(industry='', key='', status=None, page=1, size=100):
    """调用行业标准列表 API

    status: 字符串(单值)或 None(默认现行)"""
    params = {
        'current': page, 'size': size, 'key': key,
        'ministry': '', 'industry': industry,
        'pubdate': '', 'date': '',
    }
    if status:
        statuses = status if isinstance(status, list) else [status]
    else:
        statuses = ['现行']
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
        last_first_code = item_ckpt.get('first_code') if item_ckpt else None

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
                break

            if incr and last_first_code and records[0].get('code') and records[0].get('code') == last_first_code:
                _log.info(f"   {item_label}={label} 已到上次采集位置")
                found_checkpoint = True
                break

            cnt = 0
            for r in records:
                if item_count >= max_results:
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

            if found_checkpoint:
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
                on_progress(pct, f"扫描{label}({item_idx + 1}/{total_items}: 第{page}页)")

            if on_intermediate:
                on_intermediate(all_standards)

            if page >= pages:
                break
            time.sleep(DELAY)

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

    on_progress: 可选 async callable(pct, msg)，每下载一条调用一次
    on_item_done: 可选 async callable(item_name='')，每完成一条标准处理后调用（用于实时推送 stats）
    check_pause: 可选 async callable() → bool，每项处理前调用，返回 False 则中止下载
    """
    output_dir = get_output_dir()
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    existing = get_existing_files()
    to_download = [s for s in standards if s.get('pk')]

    stats = {'downloaded': 0, 'skipped_existing': 0, 'failed': 0}
    total = len(to_download)
    for i, s in enumerate(to_download):
        if check_pause:
            if not await check_pause():
                _log.info(f"{log_prefix} 下载被中止")
                break
        filename = make_filename(s['stdCode'], s['stdName'])
        filepath = output_dir / filename

        if filename in existing:
            stats['skipped_existing'] += 1
            s['dlStatus'] = 'skipped_existing'
            if on_item_done:
                await on_item_done(s.get('stdName', ''))
            continue

        display = f"{s['stdCode'][:20]} {s['stdName'][:25]}"
        _log.info(f"   [{i+1}/{len(to_download)}] {display}...")

        try:
            pdf_data = await asyncio.get_running_loop().run_in_executor(
                None, download_hb_with_captcha, s['pk'], s['siteType']
            )
            if pdf_data:
                atomic_write(str(filepath), pdf_data, dir_=str(output_dir))
                stats['downloaded'] += 1
                s['dlStatus'] = 'downloaded'
                _log.info(f"   [OK] {display} ({len(pdf_data)/1024:.0f}KB)")
                add_to_existing_files_cache(filename)
            else:
                stats['failed'] += 1
                s['dlStatus'] = 'failed'
                _log.warning(f"   [FAIL] {display}")
        except Exception as e:
            stats['failed'] += 1
            s['dlStatus'] = 'failed'
            _log.error(f"   [ERR] {display}: {e}")

        if on_item_done:
            await on_item_done(s.get('stdName', ''))

        await asyncio.sleep(DELAY)

        if on_progress and total > 0:
            await on_progress(int(100 * (i + 1) / total), f"下载中 {i + 1}/{total}")

    _log.info(f"{log_prefix} 下载:{stats['downloaded']} 跳过:{stats['skipped_existing']} 失败:{stats['failed']}")


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


def download_hb_with_captcha(hb_hash, site_type='hb'):
    """通过验证码下载行业标准/地方标准 PDF"""
    if site_type == 'hb':
        base_url = 'https://hbba.sacinfo.org.cn'
    else:
        base_url = 'https://dbba.sacinfo.org.cn'

    _download_code = [None]

    def captcha_getter(client):
        resp = client.get(f'{base_url}/portal/validate-code?pk={hb_hash}')
        resp.raise_for_status()
        return resp.content

    def captcha_verifier(client, code):
        verify_resp = client.post(
            f'{base_url}/portal/validate-captcha/down',
            data={'captcha': code, 'pk': hb_hash}
        )
        verify_resp.raise_for_status()
        result = verify_resp.json()
        code_val = result.get('code')
        if code_val == 0 or code_val == '0':
            _download_code[0] = result.get('msg', '')
            return bool(_download_code[0])
        return False

    def pdf_getter(client):
        dc = _download_code[0]
        if not dc:
            return None
        resp = client.get(f'{base_url}/portal/download/{dc}')
        resp.raise_for_status()
        return resp.content

    return _unified_captcha_download({
        'site_type': site_type,
        'captcha_getter': captcha_getter,
        'captcha_verifier': captcha_verifier,
        'pdf_getter': pdf_getter,
    })


def _ms_to_date(ms):
    """毫秒时间戳 → 日期字符串"""
    if not ms:
        return ''
    try:
        return time.strftime('%Y-%m-%d', time.localtime(ms / 1000))
    except Exception as e:
        _log.debug(f"ms_to_date 转换失败 (ms={ms}): {e}")
        return ''
