"""scanner.scan_core — 列表扫描分页骨架

统一处理翻页、增量 checkpoint、暂停、进度与中间结果推送。
各站点只负责“请求一页 + 解析成统一条目 + 条目过滤”。

增量语义：
- 有上次 checkpoint 时，一直扫到命中上次位置为止，max_results 不再是硬停点，
  避免“新增超过 max_results 时 checkpoint 前移、漏掉中间条目”。
- 为防止 checkpoint 失效导致无限翻页，设置 3 倍页数硬上限；触达上限时不推进
  checkpoint，并记录告警，下次扫描仍可补扫。
"""

import asyncio
import logging
import math
from typing import Awaitable, Callable, Optional

from app.scanner.checkpoint import get_incr_checkpoint, update_incr_checkpoint
from config.settings import get_delay

_log = logging.getLogger('std_scraper')


def make_filter_key(*parts) -> str:
    """把影响扫描结果的筛选条件拼成 checkpoint 作用域。"""
    return '|'.join(str(p or '') for p in parts)


def load_filtered_checkpoint(scan_type: str, item_key: Optional[str], filter_key: str):
    """读取 checkpoint；筛选条件变化时视为无 checkpoint（本次全量）。"""
    ckpt = get_incr_checkpoint(scan_type, item_key)
    if not ckpt:
        return None
    stored = ckpt.get('filter')
    if stored and filter_key and stored != filter_key:
        _log.info(f"[SCAN] {scan_type}/{item_key or '-'} 筛选条件已变化"
                  f"（{stored} → {filter_key}），本次按全量扫描")
        return None
    return ckpt


async def scan_pages(
    *,
    fetch_page: Callable[[int], Awaitable[list]],
    scan_type: str,
    item_key: Optional[str] = None,
    filter_key: str = '',
    incr: bool = False,
    max_results: int = 500,
    page_size: int = 50,
    item_id: Callable[[dict], str] = lambda it: str(it.get('detail_id') or it.get('id') or ''),
    item_code: Callable[[dict], str] = lambda it: it.get('code') or it.get('stdCode') or '',
    item_name: Callable[[dict], str] = lambda it: it.get('name') or it.get('stdName') or '',
    filter_item: Optional[Callable[[dict], bool]] = None,
    post_process: Optional[Callable[[list], list]] = None,
    on_progress=None,
    on_intermediate=None,
    check_pause=None,
    on_error=None,
    progress_base: int = 0,
    progress_share: float = 1.0,
    label: str = '',
    progress_text: Optional[Callable[[int, int], str]] = None,
) -> list:
    """扫描分页列表，返回过滤后的条目。

    fetch_page(page) 为 async 可调用，返回该页解析后的条目列表。
    """
    if max_results <= 0:
        return []

    ckpt = load_filtered_checkpoint(scan_type, item_key, filter_key) if incr else None
    last_id = ckpt.get('first_id') if ckpt else None
    if last_id:
        _log.info(f"{label} 增量扫描，上次位置 id={last_id}")
    all_items = []
    page = 0
    hit_last = False
    ckpt_blocked = False

    # 增量有历史位置时，允许突破 max_results 直到命中上次位置；
    # 3 倍页数作为安全上限，防止 checkpoint 失效后无限翻页。
    page_limit = None
    if incr and last_id:
        page_limit = max(30, math.ceil(max_results / max(page_size, 1)) * 3)

    while True:
        page += 1
        if page_limit and page > page_limit:
            ckpt_blocked = True
            _log.warning(f"{label} 增量扫描超过 {page_limit} 页仍未命中上次位置，"
                         f"停止且不推进 checkpoint")
            break

        if check_pause:
            try:
                if not await check_pause():
                    _log.info(f"{label} 扫描被中止")
                    return all_items
            except Exception:
                pass

        if on_progress:
            pct = min(95, int(len(all_items) / max(max_results, 1) * 95))
            pct = progress_base + int(progress_share * pct)
            msg = (progress_text(page, len(all_items)) if progress_text
                   else f'{label}第{page}页（已{len(all_items)}条）')
            try:
                await on_progress(pct, msg)
            except Exception:
                pass

        items = None
        last_exc = None
        for attempt in range(2):
            try:
                items = await fetch_page(page)
                last_exc = None
                break
            except Exception as e:
                last_exc = e
                if attempt == 0:
                    _log.warning(f"{label} 第{page}页请求失败，重试一次: {e}")
                    await asyncio.sleep(get_delay())
        if last_exc is not None:
            if on_error:
                on_error(page, last_exc)
            else:
                _log.error(f"{label} 第{page}页列表请求失败: {last_exc}")
            # 页码中断：本次结果不完整，不推进 checkpoint，避免漏掉后续页
            ckpt_blocked = True
            break

        if not items:
            _log.info(f"{label} 第{page}页无数据，结束")
            break

        for it in items:
            rid = item_id(it)
            if last_id and rid and rid == last_id:
                _log.info(f"{label} 增量命中上次位置 id={last_id}，停止")
                hit_last = True
                break
            if filter_item and not filter_item(it):
                continue
            all_items.append(it)
            if len(all_items) >= max_results and not (incr and last_id):
                break

        if hit_last:
            break
        if len(all_items) >= max_results and not (incr and last_id):
            break

        if on_intermediate:
            try:
                await on_intermediate(list(all_items))
            except Exception:
                pass

        if len(items) < page_size:
            break

        await asyncio.sleep(get_delay())

    if post_process:
        processed = post_process(all_items)
        if processed is not None:
            all_items = processed

    if all_items and not ckpt_blocked:
        update_incr_checkpoint(scan_type, item_key, {
            'first_id': item_id(all_items[0]),
            'first_code': item_code(all_items[0]),
            'first_name': item_name(all_items[0]),
            'count': len(all_items),
            'filter': filter_key,
        })

    _log.info(f"{label} 扫描完成: {len(all_items)} 条")
    return all_items
