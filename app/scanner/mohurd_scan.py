"""scanner.mohurd_scan — 工程建设标准（住建部公告）检索与下载

工程建设标准（GB 50xxx–55xxx 国家标准，以及 CJJ/JGJ 行业标准）不在
std.samr.gov.cn 检索体系内，由住房和城乡建设部在"标准公告"中发布，公告页的
附件即标准全文 PDF。

流程：
  1. jpaas-jsearch 站内检索（用紧凑编号，如 GB50016-2014）
  2. 在结果中挑选"发布国家标准/行业标准"公告（跳过英文版、征求意见、答复）
  3. 打开公告页，用正文"编号为GB..."确认编号一致
  4. 取附件直链 document/download，流式写入磁盘（PDF 常有几十 MB）

注意：
  - "局部修订条文"公告的附件可能只含修订条款，不代表全本；
  - 早期标准可能没有附件，此时标记为无全文（需购买正式出版物）。
"""

import asyncio
import json
import logging
import re
import urllib.parse

import httpx

from app.helpers import normalize_code
from app.scanner.checkpoint import get_incr_checkpoint, update_incr_checkpoint
from app.scanner.scan_core import make_filter_key
from config.settings import (
    MOHURD_BASE, MOHURD_SEARCH_API, MOHURD_SEARCH_SERVICE_ID,
    MOHURD_SEARCH_CATE_ID, http_client,
    get_delay, get_output_dir,
)

_log = logging.getLogger('std_scraper')

_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

_SEARCH_PAGE_SIZE = 20
_MAX_ANNOUNCEMENTS = 5

_ART_ID_RE = re.compile(r'art_([0-9a-f_]+)\.html', re.I)
_CODE_RE = re.compile(r'[A-Z]{1,5}(?:/T)?\s*\d{1,5}(?:\.\d+)?\s*[-—–]\s*\d{2,4}')
_ISSUE_CODE_RE = re.compile(
    r'编号为\s*([A-Z]{1,5}(?:/T)?\s*\d{1,5}(?:\.\d+)?\s*[-—–]\s*\d{2,4})')
_ATTACH_RE = re.compile(
    r'href="([^"]*document/download[^"]*)"[^>]*>([^<]*)</a>', re.S)
_PUBLISH_RE = re.compile(r'发布时间：\s*(\d{4}-\d{2}-\d{2})')
_NAME_RE = re.compile(r'《([^》]+)》')

_SKIP_TITLE_WORDS = ('英文版', '征求意见', '答复', '声明', '公示', '通知')

# 标准公告栏目列表（按发布时间倒序）
_LIST_API = ('https://www.mohurd.gov.cn/api-gateway/jpaas-publish-server'
             '/front/page/build/unit')
_LIST_BASE_PARAMS = {
    'parseType': 'bulidstatic',
    'webId': '86ca573ec4df405db627fdc2493677f3',
    'tplSetId': 'fc259c381af3496d85e61997ea7771cb',
    'pageType': 'column',
    'editType': 'null',
    'pageId': 'Y53uSscFTKvrbHxaF9Cvp',
    'tagId': '栏目-list',
}
_LIST_PAGE_SIZE = 100
_LIST_ITEM_RE = re.compile(
    r'href="([^"]+)"[^>]*title="([^"]+)"', re.S)
_LIST_ID_RE = re.compile(r'art_([0-9a-f_]+)\.html', re.I)


def _compact(code):
    """去掉空白并统一大小写，用于编号比对。"""
    return re.sub(r'\s+', '', normalize_code(str(code or ''))).upper()


def _with_space(code):
    """GB51043-2014 → GB 51043-2014，保持与检索结果一致的编号格式。"""
    norm = normalize_code(str(code or '')).strip()
    m = re.match(r'^([A-Za-z]{1,6}(?:/[A-Za-z]{1,4})?)\s*(\d.*)$', norm)
    return f'{m.group(1)} {m.group(2)}' if m else norm


def _search_announcements(code):
    """调用住建部站内检索，返回 [{title, url}]。"""
    params = {
        'serviceId': MOHURD_SEARCH_SERVICE_ID,
        'websiteid': '',
        'cateid': MOHURD_SEARCH_CATE_ID,
        'q': _compact(code),
        'pg': str(_SEARCH_PAGE_SIZE),
        'p': '1',
        'pos': '',
        'sortType': '',
    }
    headers = {
        'User-Agent': _UA,
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': f'{MOHURD_BASE}/',
    }
    resp = http_client.get(MOHURD_SEARCH_API, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    result = ((payload.get('data') or {}).get('searchResult') or {}).get('result') or []

    items, seen = [], set()
    for block in result:
        m = re.search(r'href="(https?://[^"]+)"', block)
        if not m or m.group(1) in seen:
            continue
        url = m.group(1)
        seen.add(url)
        title_match = re.search(r'data-title="([^"]+)"', block)
        if not title_match:
            title_match = re.search(r'class="textTitle"[^>]*>(.*?)</a>', block, re.S)
            title = re.sub(r'<[^>]+>', '', title_match.group(1)).strip() if title_match else ''
        else:
            title = title_match.group(1)
        items.append({'title': title, 'url': url})
    return items


def _is_announcement(item):
    """只保留文件库里的标准公告页。"""
    return '/gongkai/zc/wjk/art/' in item.get('url', '')


def _candidate_score(title):
    """公告候选优先级：正式发布 > 局部修订 > 其他。"""
    if any(w in title for w in _SKIP_TITLE_WORDS):
        return -100
    score = 0
    if '发布' in title:
        score += 2
    if '国家标准' in title or '行业标准' in title:
        score += 2
    if '局部修订' in title:
        score -= 5
    return score


def _extract_name(title):
    m = _NAME_RE.search(title or '')
    return m.group(1).strip() if m else ''


def _fetch_announcement(url, title):
    """抓取公告页，返回编号/附件/发布时间；失败返回 None。"""
    try:
        resp = http_client.get(url, headers={'User-Agent': _UA}, timeout=30)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        _log.warning(f"[MOHURD] 公告页请求失败 {url}: {type(e).__name__}: {e}")
        return None

    codes = sorted({ _with_space(c) for c in _ISSUE_CODE_RE.findall(html) })
    if not codes:
        codes = sorted({ _with_space(c) for c in _CODE_RE.findall(html) })

    attachments = []
    for href, label in _ATTACH_RE.findall(html):
        href = urllib.parse.urljoin(url, href.replace('&amp;', '&'))
        attachments.append({'url': href, 'label': re.sub(r'<[^>]+>', '', label).strip()})

    pub = _PUBLISH_RE.search(html)
    return {
        'codes': codes,
        'attachments': attachments,
        'publishDate': pub.group(1) if pub else '',
        'name': _extract_name(title),
    }


def _fetch_announcement_list(page: int, page_size: int = _LIST_PAGE_SIZE) -> list:
    """抓取标准公告列表页，返回 [{id, title, url}]（按发布时间倒序）。"""
    params = dict(_LIST_BASE_PARAMS)
    params['paramJson'] = json.dumps({'pageNo': page, 'pageSize': page_size})
    headers = {
        'User-Agent': _UA,
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Referer': f'{MOHURD_BASE}/gongkai/fdzdgknr/bzgg/index.html',
    }
    resp = http_client.get(_LIST_API, params=params, headers=headers, timeout=30)
    resp.raise_for_status()
    html = ((resp.json().get('data') or {}).get('html') or '')

    items, seen = [], set()
    for href, title in _LIST_ITEM_RE.findall(html):
        if 'art_' not in href or href in seen:
            continue
        seen.add(href)
        m = _LIST_ID_RE.search(href)
        items.append({
            'id': m.group(1) if m else href,
            'title': re.sub(r'<[^>]+>', '', title).strip(),
            'url': urllib.parse.urljoin(MOHURD_BASE, href),
        })
    return items


def _build_standard_items(ann: dict, info: dict) -> list:
    """把一条公告展开成标准条目；批量公告只给首个编号挂附件。"""
    codes = info.get('codes') or []
    if not codes:
        return []
    attachments = info.get('attachments') or []
    name = info.get('name') or ann.get('title', '')
    revision = '局部修订' in ann.get('title', '')
    items = []
    for idx, code in enumerate(codes):
        # 批量公告通常只有一个合并附件，挂到首个编号，避免重复下载同一文件
        attachment = attachments[0] if (idx == 0 and attachments) else None
        msg = '住建部公告附件' if attachment else '公告未提供附件'
        if revision:
            msg += '（局部修订，可能仅含修订条文）'
        items.append({
            'code': code,
            'name': name,
            'tid': 'BV_MOHURD',
            'pid': ann['id'],
            'detail_id': f"{ann['id']}:{_compact(code)}",
            'status': '现行',
            'publishDate': info.get('publishDate', ''),
            'detailUrl': ann['url'],
            'pdf_url': attachment['url'] if attachment else '',
            'downloadable': True,
            'download_msg': msg,
        })
    return items


async def scan_mohurd_standards(
    max_results: int = 500,
    incr: bool = False,
    keyword_group: str = '安全生产',
    status: str = '全部',
    on_progress=None,
    on_intermediate=None,
    check_pause=None,
    on_error=None,
) -> list:
    """扫描住建部标准公告，按公告正文中的编号产出标准条目。"""
    if max_results <= 0:
        return []

    # 工程建设标准按公告全量采集，不套用安全关键词组，避免漏掉非安全类标准
    filter_key = make_filter_key('mohurd')

    ckpt = get_incr_checkpoint('mohurd') if incr else None
    if ckpt and ckpt.get('filter') and ckpt['filter'] != filter_key:
        _log.info(f"[MOHURD] 筛选条件已变化（{ckpt['filter']} → {filter_key}），本次按全量扫描")
        ckpt = None
    last_id = ckpt.get('first_id') if ckpt else None

    max_pages = max(30, max_results // _LIST_PAGE_SIZE + 3)
    if incr and last_id:
        max_pages *= 3

    standards, seen_codes = [], set()
    first_ann = None
    hit_last = ended = page_failed = detail_failed = False
    page = 0

    while page < max_pages:
        page += 1
        if check_pause:
            try:
                if not await check_pause():
                    return standards
            except Exception:
                pass

        if on_progress:
            pct = min(95, int(len(standards) / max(max_results, 1) * 95))
            try:
                await on_progress(pct, f'扫描公告第{page}页（已{len(standards)}条）')
            except Exception:
                pass

        loop = asyncio.get_running_loop()
        anns, last_exc = None, None
        for attempt in range(2):
            try:
                anns = await loop.run_in_executor(None, _fetch_announcement_list, page)
                last_exc = None
                break
            except Exception as e:
                last_exc = e
                if attempt == 0:
                    _log.warning(f"[MOHURD] 第{page}页请求失败，重试一次: {e}")
                    await asyncio.sleep(get_delay())
        if last_exc is not None:
            if on_error:
                on_error(page, last_exc)
            else:
                _log.error(f"[MOHURD] 第{page}页请求失败: {last_exc}")
            page_failed = True
            break
        if not anns:
            ended = True
            break

        for ann in anns:
            if last_id and ann['id'] == last_id:
                hit_last = True
                break
            if any(w in ann['title'] for w in _SKIP_TITLE_WORDS):
                continue
            if first_ann is None:
                first_ann = ann

            info = await loop.run_in_executor(
                None, _fetch_announcement, ann['url'], ann['title'])
            if not info:
                detail_failed = True
                break
            if not info.get('codes') and any(
                    k in ann['title'] for k in ('国家标准', '行业标准')):
                _log.warning(f"[MOHURD] 公告未解析出编号，稍后重试: {ann['title']}")
                detail_failed = True
                break

            for item in _build_standard_items(ann, info):
                if item['code'] in seen_codes:
                    continue
                if status and status != '全部' and item['status'] != status:
                    continue
                seen_codes.add(item['code'])
                standards.append(item)
                if len(standards) >= max_results:
                    break
            if detail_failed or len(standards) >= max_results:
                break
            # 详情页逐条抓取，遵守站点请求间隔
            await asyncio.sleep(get_delay())

        if hit_last or page_failed or detail_failed or len(standards) >= max_results:
            break
        if on_intermediate:
            try:
                await on_intermediate(list(standards))
            except Exception:
                pass
        if len(anns) < _LIST_PAGE_SIZE:
            ended = True
            break
        await asyncio.sleep(get_delay())

    blocked = bool(page_failed or detail_failed or (
        incr and last_id and not hit_last and not ended))
    if blocked:
        _log.warning("[MOHURD] 本轮结果不完整，checkpoint 未推进")
    elif first_ann:
        update_incr_checkpoint('mohurd', None, {
            'first_id': first_ann['id'],
            'first_code': first_ann['title'],
            'first_name': first_ann['title'],
            'count': len(standards),
            'filter': filter_key,
        })

    _log.info(f"[MOHURD] 扫描完成: {len(standards)} 条")
    return standards


def find_mohurd_standard(code):
    """按标准编号查找住建部公告，返回可入库的候选 dict；找不到返回 None。"""
    target = _compact(code)
    if not target:
        return None

    try:
        results = _search_announcements(code)
    except Exception as e:
        raise RuntimeError(f'住建部检索失败: {type(e).__name__}: {e}') from e

    candidates = [r for r in results if _is_announcement(r)]
    candidates.sort(key=lambda r: _candidate_score(r.get('title') or ''), reverse=True)

    # 收集所有编号匹配的公告，优先选带附件、且非"局部修订"的
    matches = []
    for cand in candidates[:_MAX_ANNOUNCEMENTS]:
        if _candidate_score(cand.get('title') or '') <= -100:
            continue
        info = _fetch_announcement(cand['url'], cand['title'])
        if not info:
            continue
        if target not in {_compact(c) for c in info['codes']}:
            continue
        matches.append((cand, info))
        if info['attachments'] and '局部修订' not in cand['title']:
            break  # 已找到正式发布且有附件的公告，无需继续
    if not matches:
        return None

    matches.sort(key=lambda m: (bool(m[1]['attachments']),
                                '局部修订' not in m[0]['title']), reverse=True)
    cand, info = matches[0]
    revision = '局部修订' in cand['title']
    attachment = info['attachments'][0] if info['attachments'] else None
    art = _ART_ID_RE.search(cand['url'])
    item = {
        'code': _with_space(code),
        'name': info['name'] or (attachment['label'] if attachment else ''),
        'tid': 'BV_MOHURD',
        'pid': art.group(1) if art else cand['url'],
        'status': '',
        'publishDate': info['publishDate'],
        'detailUrl': cand['url'],
        'pdf_url': attachment['url'] if attachment else '',
        'downloadable': True,
        'download_msg': '住建部公告附件' if attachment else '公告未提供附件',
    }
    if revision:
        item['download_msg'] += '（局部修订，可能仅含修订条文）'
    return item


class _Aborted(Exception):
    """下载被暂停/删除中断。"""


def download_mohurd_pdf(pdf_url, filepath, referer='', abort_check=None):
    """流式下载住建部公告附件 PDF，返回 (ok, reason)。

    PDF 常有几十 MB，不能像小文件一样整块读入内存；先写 .part 再改名，
    避免中断后留下半个文件被去重逻辑误判为已存在。
    """
    if abort_check and abort_check():
        return False, 'aborted'
    tmp = filepath.with_name(filepath.name + '.part')
    headers = {'User-Agent': _UA}
    if referer:
        headers['Referer'] = referer
    try:
        with http_client.stream('GET', pdf_url, headers=headers,
                                timeout=httpx.Timeout(300.0, connect=30.0)) as resp:
            resp.raise_for_status()
            content_type = resp.headers.get('content-type', '')
            first = b''
            with open(tmp, 'wb') as f:
                for chunk in resp.iter_bytes(256 * 1024):
                    if abort_check and abort_check():
                        raise _Aborted()
                    if not first:
                        first = chunk[:5]
                    f.write(chunk)
        if first != b'%PDF-':
            tmp.unlink(missing_ok=True)
            return False, f'响应非PDF(ct={content_type})'
        if tmp.stat().st_size < 1024:
            tmp.unlink(missing_ok=True)
            return False, '文件过小'
        tmp.replace(filepath)
        return True, ''
    except _Aborted:
        tmp.unlink(missing_ok=True)
        return False, 'aborted'
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False, f'{type(e).__name__}: {e}'


async def _download_one_mohurd(std, existing, check_pause=None):
    """下载单条住建部公告附件，更新 dlStatus。"""
    from app.scanner.utils import make_filename

    if check_pause:
        try:
            if not await check_pause():
                std['dlStatus'] = 'aborted'
                return std
        except Exception:
            pass

    code = std.get('code', '')
    name = std.get('name', '')
    filename = make_filename(code, name, suffix='.pdf')
    filepath = get_output_dir() / filename

    if filename.lower() in existing or filepath.exists():
        std['dlStatus'] = 'skipped_existing'
    elif not std.get('pdf_url'):
        std['dlStatus'] = 'skipped'
        std['failReason'] = std.get('download_msg') or '公告未提供附件'
    else:
        ok, reason = await asyncio.get_running_loop().run_in_executor(
            None, download_mohurd_pdf, std['pdf_url'], filepath,
            std.get('detailUrl', ''))
        if ok:
            std['dlStatus'] = 'downloaded'
            std.pop('failReason', None)
            existing.add(filename.lower())
        elif reason == 'aborted':
            std['dlStatus'] = 'aborted'
        else:
            std['dlStatus'] = 'failed'
            std['failReason'] = reason
            _log.info(f"  [MOHURD][FAIL] {reason}: {code}")
            await asyncio.sleep(get_delay())
    return std


async def download_mohurd_standards(
    standards: list,
    on_progress=None,
    on_item_done=None,
    check_pause=None,
):
    """下载住建部标准公告附件（大文件流式写入）。"""
    from app.dedup import get_existing_files
    from config.manager import load_config

    existing = get_existing_files()
    total = len(standards)
    concurrent = max(1, min(10, int(load_config().get('download', {}).get('concurrent', 1))))
    sem = asyncio.Semaphore(concurrent)
    done = [0]
    lock = asyncio.Lock()

    async def _one(std):
        async with sem:
            await _download_one_mohurd(std, existing, check_pause)
            async with lock:
                done[0] += 1
                idx = done[0]
                if on_item_done:
                    await on_item_done(f"{std.get('code', '')} {std.get('name', '')[:20]}")
                if on_progress and total:
                    await on_progress(int(100 * idx / total), f'下载中 {idx}/{total}')

    if standards:
        await asyncio.gather(*[_one(s) for s in standards])


async def run_mohurd_pipeline(
    config: dict,
    task_id: str = None,
    task_manager=None,
    progress_base: int = 0,
    progress_per_scan: int = 40,
    progress_per_download: int = 60,
) -> list:
    """工程建设标准统一管线：扫描住建部公告 + 下载附件。"""
    from app.scanner.change_tracker import compare_snapshot
    from app.scanner.utils import (
        compute_download_stats, format_download_summary, ScanErrorCollector,
    )

    max_results = config.get('max_results', 500)
    incr = config.get('incr', False)
    keyword_group = config.get('keyword_group', '安全生产')
    scan_only = config.get('scan_only', False)
    std_state = config.get('std_state') or '全部'

    scan_errors = ScanErrorCollector(task_manager, task_id, '[MOHURD]')
    scan_errors.reset()

    async def _on_scan_progress(pct, msg):
        if task_manager and task_id:
            task_manager.update(
                task_id,
                progress=progress_base + max(1, int(pct * progress_per_scan / 100)),
                scan_progress=pct, message=msg, persist_std_items=False)

    async def _on_dl_progress(pct, msg):
        if task_manager and task_id:
            task_manager.update(
                task_id,
                progress=progress_base + progress_per_scan
                + max(1, int(pct * progress_per_download / 100)),
                dl_progress=pct, message=msg, persist_std_items=False)

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

    counter = [0]

    async def _on_intermediate(items):
        if task_manager and task_id:
            counter[0] += 1
            task_manager.update(
                task_id, std_items=list(items), stats={'scanned': len(items)},
                persist_std_items=counter[0] % 3 == 0)

    item_counter = [0]

    async def _on_item_done(name=''):
        item_counter[0] += 1
        if task_manager and task_id:
            stats = compute_download_stats(standards)
            kwargs = {'stats': stats, 'persist_std_items': False,
                      'currentItem': name}
            if item_counter[0] % 5 == 0:
                kwargs['std_items'] = standards
                kwargs['persist_std_items'] = True
            task_manager.update(task_id, **kwargs)

    if task_manager and task_id:
        task_manager.update(task_id, progress=progress_base + 1, scan_progress=0,
                            message='扫描中工程建设标准...')

    standards = await scan_mohurd_standards(
        max_results=max_results, incr=incr, keyword_group=keyword_group,
        status=std_state, on_progress=_on_scan_progress,
        on_intermediate=_on_intermediate, check_pause=_check_pause,
        on_error=scan_errors.callback)

    scan_note = scan_errors.note()
    if task_manager and task_id:
        task_manager.update(
            task_id, progress=progress_base + progress_per_scan,
            scan_progress=100,
            message=f'工程建设标准扫描完成({len(standards)}条)'
                    + scan_errors.message_suffix(),
            scan_note=scan_note, stats={'scanned': len(standards)},
            std_items=standards)

    try:
        changes = compare_snapshot('mohurd', standards)
        if task_manager and task_id and (
                changes.get('added') or changes.get('changed') or changes.get('removed')):
            task_manager.update(task_id, changes=changes)
    except Exception as e:
        _log.debug(f"[MOHURD] 变更追踪异常: {e}")

    if not standards:
        scan_errors.raise_if_empty(standards)
        if task_manager and task_id:
            empty_msg = '无新增标准' if incr else '无符合条件标准'
            task_manager.update(
                task_id,
                progress=progress_base + progress_per_scan + progress_per_download,
                dl_progress=100,
                message=f'{empty_msg}，跳过下载',
                stats={'scanned': 0, 'downloaded': 0, 'success': 0,
                       'failed': 0, 'skipped': 0},
                std_items=[])
        return standards

    if scan_only:
        return standards

    if task_manager and task_id:
        task_manager.update(task_id,
                            progress=progress_base + progress_per_scan + 1,
                            dl_progress=0, message='下载工程建设标准...')

    await download_mohurd_standards(
        standards, on_progress=_on_dl_progress,
        on_item_done=_on_item_done, check_pause=_check_pause)

    if task_manager and task_id:
        stats = compute_download_stats(standards)
        task_manager.update(
            task_id,
            progress=progress_base + progress_per_scan + progress_per_download,
            dl_progress=100,
            message=format_download_summary(stats, prefix='工程建设标准下载'),
            stats=stats, std_items=standards)

    _log.info(f"[MOHURD] 下载完成: {len(standards)} 条")
    return standards
