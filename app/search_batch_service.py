"""search_batch_service — 批量搜索/下载的业务编排

从 HTTP 路由层抽离的可复用服务：
  - 批量搜索（含意图过滤、去重、本地已有标记、诊断记录）
  - 搜索结果批量下载（含暂停/恢复、进度回写）

路由层只负责参数校验与响应，task_manager 由调用方显式传入，
避免服务层反向依赖 app.routes。
"""

import asyncio
import logging
import time

_log = logging.getLogger('std_scraper')


async def wait_if_running(task_manager, task_id):
    """暂停时阻塞等待，返回 False 表示任务已结束/删除，应停止执行。"""
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


def is_download_only(task_manager, task_id):
    """批量搜索任务是否被要求"结束搜索、直接下载已有结果"。"""
    task = task_manager.get(task_id)
    return bool(task and task.get('_download_only'))


async def sleep_with_pause(task_manager, task_id, seconds):
    """可中断延迟：暂停时立即挂起，任务删除/结束时返回 False。"""
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        if not await wait_if_running(task_manager, task_id):
            return False
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return True
        await asyncio.sleep(min(0.2, remaining))


def _item_key(item):
    """候选项去重 key（与批量搜索入库时保持同一口径）。"""
    tid = item.get('tid', '')
    pid = item.get('pid', '')
    if tid == 'BV_TT':
        return f"BV_TT|{pid}"
    return f"{tid}|{pid}" if pid else f"{tid}|{item.get('code', '')}|{item.get('name', '')}"


async def batch_search_dedup(task_manager, task_id, queries, std_type, per_query_max,
                             exact_match=True, skip_obsolete=True,
                             start_index=0, existing_items=None, diag_seed=None,
                             total_count=None):
    """批量搜索并去重，返回 (候选 items, 搜索阶段跳过的废止条数, 诊断记录器)。

    每搜完一个关键词就把已匹配结果和诊断信息写回任务，前端可实时看到
    "这个查询站点返回几条、为什么没有新增"，而不是只看到一个 +0。

    exact_match=True 时按查询意图过滤：标准号行只保留编号匹配的结果
    （未指定年份时自动排除废止版本），关键词行仍按 per_query_max 截断。

    skip_obsolete=True 时，关键词命中的废止版本直接跳过（标准号行不受影响，
    用户明确查询某个废止编号时仍然保留）。

    断点续跑：start_index 表示前 N 个查询已完成，existing_items/diag_seed
    为上次已持久化的候选与诊断，用于续跑的增量去重与统计接续。
    total_count 为完整查询数（默认按本批 queries 计算），仅影响进度显示。

    去重 key：GB 用 pid；HB/DB 用 code+name（pid 可能跨类型相同）；
    TT（团体标准）用 detail_id。
    """
    from app.scanner import fetch_stdpage_search, check_downloadable, search_tt_standards
    from app.scanner.search import filter_standard_results, parse_standard_code
    from app.scanner.search_diag import SearchBatchDiag
    from app.scanner.utils import compute_download_stats, make_filename
    from app.dedup import get_existing_files
    from config.settings import DETAIL_URL, get_delay

    all_items = list(existing_items or [])
    seen = {_item_key(item) for item in all_items}
    obsolete_skipped = int((diag_seed or {}).get('obsolete_total') or 0)
    total = total_count or (start_index + len(queries))
    loop = asyncio.get_running_loop()
    is_tt = (std_type == '团体标准')
    # 搜索阶段先做本地去重：已有文件的条目直接标记，后续不再尝试下载
    existing = await loop.run_in_executor(None, get_existing_files)
    diag = SearchBatchDiag(total, seed=diag_seed)
    aborted = False

    for offset, q in enumerate(queries):
        i = start_index + offset
        # 暂停时挂起；任务被删除/结束时中止搜索
        if not await wait_if_running(task_manager, task_id):
            _log.info(f"任务 {task_id}: 搜索被中止于 {i}/{total}")
            break
        # 用户点击"下载已有结果"时立即结束剩余搜索
        if is_download_only(task_manager, task_id):
            _log.info(f"任务 {task_id}: 收到下载指令，结束剩余 {total - i} 个关键词搜索")
            break
        task_manager.update(task_id,
                            progress=int(50 * i / total) if total else 50,
                            message=f"搜索中 {i+1}/{total}: {q[:30]}（累计候选 {len(all_items)}）")

        raw_standards, site_total, query_error = [], 0, ''
        try:
            if is_tt:
                # 团体标准走 httpx 直连 API 搜索
                raw_standards = await search_tt_standards(q, per_query_max)
                site_total = len(raw_standards)
            else:
                raw_standards, site_total = await loop.run_in_executor(
                    None, fetch_stdpage_search, q, 1, std_type)
        except Exception as e:
            query_error = f'{type(e).__name__}: {e}'

        raw_count = len(raw_standards)
        standards = list(raw_standards)
        is_code_query = False
        obsolete_this = 0

        if not query_error:
            if exact_match:
                standards, is_code_query = filter_standard_results(q, standards, per_query_max)
            else:
                standards = standards[:per_query_max]

            # 编号查询未命中时，用数字部分再搜一次（站点对完整编号的匹配不稳定）
            if is_code_query and not standards:
                info = parse_standard_code(q)
                retry_q = (info['part'] + (f"-{info['year']}" if info['year'] else '')) if info else ''
                if retry_q and retry_q != q.strip():
                    try:
                        if is_tt:
                            retry_standards = await search_tt_standards(retry_q, per_query_max)
                        else:
                            retry_standards, retry_total = await loop.run_in_executor(
                                None, fetch_stdpage_search, retry_q, 1, std_type)
                            site_total = max(site_total, retry_total)
                        standards, _ = filter_standard_results(q, retry_standards, per_query_max)
                        _log.info(f"  [{i+1}/{total}] '{q[:40]}' 完整编号未命中，"
                                  f"改用 '{retry_q}' 二次搜索（站点 {len(retry_standards)} 条，"
                                  f"编号匹配 {len(standards)} 条）")
                    except Exception as e:
                        _log.warning(f"  [{i+1}/{total}] 按编号二次搜索 '{retry_q}' 失败: "
                                     f"{type(e).__name__}: {e}")

            # 关键词命中的废止版本默认跳过；标准号查询保持原样（用户可能就是要旧版）
            if skip_obsolete and not is_code_query and standards:
                kept = [s for s in standards if '废止' not in (s.get('status') or '')]
                obsolete_this = len(standards) - len(kept)
                obsolete_skipped += obsolete_this
                standards = kept

        matched_count = len(standards)
        added = duplicate = existing_count = unavailable = 0
        for s in standards:
            # 暂停时立即挂起；任务删除/结束时停止批量搜索
            if not await wait_if_running(task_manager, task_id):
                aborted = True
                break
            if is_download_only(task_manager, task_id):
                aborted = True
                break
            if is_tt:
                # 团体标准结果格式
                tid = 'BV_TT'
                pid = s.get('detail_id', '')
                code = s.get('code', '')
                name = s.get('name', '')
                detail_id = pid
                key = f"BV_TT|{detail_id}"
                if key in seen:
                    duplicate += 1
                    continue
                seen.add(key)
                # 付费标准跳过
                if s.get('sellable'):
                    unavailable += 1
                    continue
                item = {
                    "code": code,
                    "name": name,
                    "tid": tid,
                    "pid": pid,
                    "status": s.get('status', ''),
                    "publishDate": s.get('publishDate', ''),
                    "actDate": '',
                    "downloadable": True,
                    "download_msg": '支持下载',
                    "detailUrl": f"https://www.ttbz.org.cn/StandardManage/Detail/{detail_id}/",
                    "query": q,
                }
                if make_filename(code, name) in existing:
                    item['dlStatus'] = 'skipped_existing'
                    existing_count += 1
                all_items.append(item)
                added += 1
                continue

            tid = s.get('tid', '')
            pid = s.get('pid', '')
            code = s.get('code', '')
            name = s.get('name', '')
            # 去重 key：GB 用 pid（30 char hex）；HB/DB tid 是 BV_HB/BV_DB，pid 是 64 位 hash
            key = f"{tid}|{pid}" if pid else f"{tid}|{code}|{name}"
            if key in seen:
                duplicate += 1
                continue
            seen.add(key)

            can_dl, dl_msg = await loop.run_in_executor(None, check_downloadable, tid)
            if not can_dl:
                unavailable += 1
                continue
            item = {
                "code": code,
                "name": name,
                "tid": tid,
                "pid": pid,
                "status": s.get('status', ''),
                "publishDate": s.get('publishDate', ''),
                "actDate": s.get('actDate', ''),
                "downloadable": can_dl,
                "download_msg": dl_msg,
                "detailUrl": f"{DETAIL_URL}?id={tid}",
                "query": q,  # 记录命中的关键词，便于审计
            }
            if make_filename(code, name) in existing:
                item['dlStatus'] = 'skipped_existing'
                existing_count += 1
            all_items.append(item)
            added += 1

        # 当前查询被暂停/中断：不记录该查询，续跑时会重新搜索它
        if aborted:
            break

        reason = diag.record(q, raw=raw_count, matched=matched_count, added=added,
                             duplicate=duplicate, existing=existing_count,
                             unavailable=unavailable, obsolete=obsolete_this,
                             site_total=site_total, error=query_error,
                             sample_codes=[s.get('code', '') for s in raw_standards[:3]])

        # 每个查询输出一行可读日志：站点返回多少、匹配多少、为什么没有新增
        if query_error:
            _log.warning(f"  [{i+1}/{total}] {q[:40]} → 站点 0 条，候选 +0：{reason}"
                         f"（累计 {len(all_items)}）")
        elif added:
            extra = f"，其中本地已有 {existing_count}" if existing_count else ''
            _log.info(f"  [{i+1}/{total}] {q[:40]} → 站点 {raw_count} 条/匹配 {matched_count} 条，"
                      f"候选 +{added}{extra}（累计 {len(all_items)}）")
        else:
            _log.info(f"  [{i+1}/{total}] {q[:40]} → 站点 {raw_count} 条/匹配 {matched_count} 条，"
                      f"候选 +0：{reason}（累计 {len(all_items)}）")

        msg = (f"搜索中 {i+1}/{total}: {q[:24]} → " +
               (f"候选 +{added}（累计 {len(all_items)}）" if added
                else f"0 新增：{reason}（累计 {len(all_items)}）"))
        # 连续无结果时在任务消息中持续提示，避免用户错过一次性的告警
        if diag.consecutive_empty >= 10:
            msg += f"｜连续 {diag.consecutive_empty} 词无结果（疑似限流）"

        # 实时回写：候选列表 + 统计 + 搜索诊断，前端展开详情即可看到。
        # std_items 较大，每 10 个查询（或最后一个）才全量持久化；search_done_count
        # 与之一同写入，确保中断续跑时"已完成查询数"与"已保存候选"始终对齐。
        persist = (offset % 10 == 9) or (offset == len(queries) - 1)
        write_kwargs = dict(
            std_items=all_items,
            progress=int(50 * (i + 1) / total) if total else 50,
            message=msg,
            stats={**compute_download_stats(all_items), 'searched': diag.done},
            search_diag=diag.to_dict(),
        )
        if persist:
            write_kwargs['search_done_count'] = i + 1
        task_manager.update(task_id, persist_std_items=persist, **write_kwargs)
        # 保护搜索站点（可被暂停/删除中断）
        if not await sleep_with_pause(task_manager, task_id, get_delay()):
            break

    # 搜索结束（正常完成/暂停/删除/转下载）后落一次盘，保证续跑起点与候选一致
    task_manager.update(task_id,
                        std_items=all_items,
                        search_done_count=start_index + diag.done,
                        search_diag=diag.to_dict(),
                        stats={**compute_download_stats(all_items), 'searched': diag.done})
    _log.info(f"任务 {task_id}: 搜索阶段结束，{diag.overview()}，"
              f"废止跳过 {obsolete_skipped} 条")
    return all_items, obsolete_skipped, diag


async def run_batch_search_pipeline(task_manager, task_id):
    """执行或续跑批量搜索任务：搜索剩余关键词 → 下载 → 完成/通知。

    任务字段约定（创建时写入、运行中持续更新）：
      search_queries     完整关键词列表
      search_done_count  已完成的查询数，续跑时从这里继续
      search_diag        已持久化的诊断计数，续跑时接续统计
      std_items          已匹配候选，续跑时用于跨查询去重
    """
    from app.scanner.utils import compute_download_stats, format_download_summary
    from app.helpers import format_duration
    from app.task_service import (
        complete_task, fail_task, notify_task_done, notify_task_failed,
    )

    started = time.time()
    task = task_manager.get(task_id) or {}
    queries = list(task.get('search_queries') or [])
    std_type = task.get('search_std_type') or '全部'
    per_query_max = int(task.get('search_per_query_max') or 10)
    exact_match = bool(task.get('search_exact_match', True))
    skip_obsolete = bool(task.get('search_skip_obsolete', True))
    auto_download = bool(task.get('search_auto_download', True))
    start_index = int(task.get('search_done_count') or 0)
    remaining = queries[start_index:]

    try:
        _log.info(f"任务 {task_id}: 批量搜索 {len(queries)} 个关键词"
                  f"（类型={std_type} 每词上限={per_query_max} 精确匹配={exact_match} "
                  f"跳过废止={skip_obsolete} 自动下载={auto_download} "
                  f"续跑起点={start_index}）")
        if remaining:
            all_items, obsolete_skipped, diag = await batch_search_dedup(
                task_manager, task_id, remaining, std_type, per_query_max,
                exact_match, skip_obsolete, start_index=start_index,
                existing_items=task.get('std_items') or [],
                diag_seed=task.get('search_diag'),
                total_count=len(queries))
        else:
            # 搜索已全部完成（中断在下载阶段），直接沿用已保存的候选
            from app.scanner.search_diag import SearchBatchDiag
            all_items = list(task.get('std_items') or [])
            diag = SearchBatchDiag(len(queries), seed=task.get('search_diag') or {})
            obsolete_skipped = diag.obsolete_total
            _log.info(f"任务 {task_id}: 搜索已全部完成，继续处理 {len(all_items)} 个候选")

        obsolete_note = f"（搜索阶段跳过废止 {obsolete_skipped} 条）" if obsolete_skipped else ''
        download_only = bool((task_manager.get(task_id) or {}).get('_download_only'))

        # 暂停时等待用户继续，避免直接进入下载阶段或提前标记完成
        if not await wait_if_running(task_manager, task_id):
            return

        if not all_items:
            msg = f"搜索完成，未找到可下载的标准｜{diag.brief()}" + obsolete_note
            complete_task(task_manager, task_id, msg, search_phase='done')
            _log.info(f"任务 {task_id}: 搜索完成（无结果），耗时 {format_duration(time.time() - started)}，"
                      f"{diag.overview()}")
            await notify_task_done(task_manager, task_id, msg,
                                   compute_download_stats(all_items))
            return

        stats = compute_download_stats(all_items)
        pending = stats.get('pending', 0)
        existing_note = f"，本地已有 {stats['skipped_existing']} 条" if stats.get('skipped_existing') else ''
        _log.info(f"任务 {task_id}: 去重后 {len(all_items)} 个标准，待下载 {pending}，"
                  f"auto_download={auto_download} download_only={download_only}")

        if auto_download or download_only:
            if pending == 0:
                msg = (f"搜索完成，{len(all_items)} 条均无需下载（本地已有或已废止）"
                       f"｜{diag.brief()}{obsolete_note}")
                complete_task(task_manager, task_id, msg, stats=stats, search_phase='done')
                await notify_task_done(task_manager, task_id, msg, stats)
                return
            prefix = '已结束搜索，开始下载' if download_only else '搜索完成，开始下载'
            task_manager.update(task_id, progress=50, search_phase='downloading',
                                message=f"{prefix} {pending} 个标准{existing_note}（{diag.brief()}）...")
            await download_selected_items(task_manager, task_id, all_items, progress_base=50)
            if not await wait_if_running(task_manager, task_id):
                return
            stats = compute_download_stats(all_items)
            final_msg = format_download_summary(stats) + f"｜{diag.brief()}" + obsolete_note
            complete_task(task_manager, task_id, final_msg, stats=stats, search_phase='done')
            await notify_task_done(task_manager, task_id, final_msg, stats)
        else:
            # 仅搜索：把结果存入 std_items 供前端查看
            msg = (f"搜索完成，候选 {len(all_items)} 条，待下载 {pending}{existing_note}"
                   f"｜{diag.brief()}{obsolete_note}")
            complete_task(task_manager, task_id, msg, stats=stats,
                          std_items=all_items, search_phase='done')
            await notify_task_done(task_manager, task_id, msg, stats)
        _log.info(f"任务 {task_id}: 完成，耗时 {format_duration(time.time() - started)}，"
                  f"{diag.overview()}")
    except Exception as e:
        _log.error(f"任务 {task_id} 异常: {e}", exc_info=True)
        fail_task(task_manager, task_id, e)
        await notify_task_failed(task_manager, task_id, e, title='搜索任务失败')


async def download_selected_items(task_manager, task_id, items, progress_base=0):
    """下载搜索结果并回写每条标准的下载状态。

    progress_base 控制进度条起始百分比：
    - 0  用于 /api/search/download 与任务一键下载（0-99%）
    - 50 用于批量搜索（50-99%，前半段是搜索）

    已处于终态的标准（已下载/已存在/版权限制等）会直接跳过，
    因此同一批结果可以安全地重复触发下载。
    """
    from app.scanner.search_download import SearchDownloadSession, FINAL_STATUSES
    from app.scanner.utils import compute_download_stats
    from config.settings import get_output_dir, get_delay

    total = len(items)
    span = 99 - progress_base
    session = SearchDownloadSession(get_output_dir())

    def _should_abort():
        """暂停或任务被删除时中断正在进行的下载"""
        task = task_manager.get(task_id)
        return task is None or task.get('status') == 'paused'

    # 先把待下载列表挂到任务上，前端展开详情即可看到完整列表
    task_manager.update(task_id, std_items=items,
                        stats=compute_download_stats(items),
                        persist_std_items=False)
    try:
        for i, item in enumerate(items):
            if not await wait_if_running(task_manager, task_id):
                _log.info(f"任务 {task_id}: 下载被中止")
                return

            code = item.get('code') or item.get('stdCode', '')
            name = item.get('name') or item.get('stdName', '')
            item_label = f"{code} {name}".strip()
            is_done = item.get('dlStatus') in FINAL_STATUSES
            dl_pct = progress_base + int(span * i / total) if total else 99
            task_manager.update(task_id, progress=dl_pct,
                                message=f"[{i+1}/{total}] {'跳过' if is_done else '处理'}: {item_label}",
                                currentItem=item_label)

            if is_done:
                continue

            _log.info(f"[{i+1}/{total}] 处理: {item_label}")
            # 被暂停/删除时下载会返回 aborted，恢复后重试当前项
            while True:
                ok, status, reason = await session.download_one(item, abort_check=_should_abort)
                if status != 'aborted':
                    break
                if not await wait_if_running(task_manager, task_id):
                    return
            if not ok:
                _log.info(f"  [FAIL] {status}" + (f": {reason}" if reason else ""))
                if not await sleep_with_pause(task_manager, task_id, get_delay()):
                    return

            # 每处理一条立即回写，列表中的下载状态实时刷新
            task_manager.update(task_id, std_items=items,
                                stats=compute_download_stats(items),
                                persist_std_items=False)
    finally:
        await session.close()
        task_manager.update(task_id, std_items=items,
                            stats=compute_download_stats(items), currentItem='')
        # 下载完成后回写统计（供卡片与通知使用）
        _log.info(f"任务 {task_id}: 下载阶段结束 {compute_download_stats(items)}")
