"""API routes — Search"""
from fastapi import APIRouter, HTTPException, Request
import time
import asyncio
import logging

from ._utils import launch_task as _launch_task, update_task_status
from .state import task_manager

# 以下模块均为搜索/下载时才需要的重模块（scanner / captcha / dedup 链），
# 统一延迟到首次调用时导入，避免阻塞 API 启动。
# 涉及：app.scanner.*, app.dedup, config.settings (http_client/OPENSTD/DETAIL_URL)

_log = logging.getLogger('std_scraper')

# 搜索支持的标准类型（与前端下拉保持一致；非法值直接拒绝，避免静默返回空结果）
_SEARCH_STD_TYPES = ('全部', '国家标准', '行业标准', '地方标准', '团体标准', '国家标准计划')


def _dedupe_queries(lines):
    """按大小写/空白归一化去重，保留首次出现的原始写法。返回 (queries, 重复数)。"""
    seen = set()
    result = []
    duplicates = 0
    for line in lines:
        key = ' '.join(line.upper().split())
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        result.append(line)
    return result, duplicates


router = APIRouter(prefix="", tags=["Search"])

@router.post("/api/search/query")
async def search_query_api(request: Request):
    from app.scanner import (
        fetch_stdpage_search, check_downloadable,
        search_tt_standards,
    )
    from config.settings import DETAIL_URL

    body = await request.json()
    query = str(body.get("query", "") or "").strip()
    std_type = body.get("std_type", "全部")
    try:
        max_results = int(body.get("max_results", 20))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="max_results 必须是整数")

    if not query:
        raise HTTPException(status_code=400, detail="搜索关键词不能为空")
    if std_type not in _SEARCH_STD_TYPES:
        raise HTTPException(status_code=400, detail=f"不支持的标准类型: {std_type}")
    if not 1 <= max_results <= 100:
        raise HTTPException(status_code=400, detail="max_results 取值范围 1-100")

    # 搜索阶段先去重：本地已有文件直接标记，避免用户误以为还要重新下载
    from app.dedup import get_existing_files
    from app.scanner.utils import make_filename
    loop = asyncio.get_running_loop()
    existing = await loop.run_in_executor(None, get_existing_files)

    # 团体标准走独立 API 搜索（ttbz.org.cn，无 WAF，httpx 直接调用）
    if std_type == '团体标准':
        try:
            standards = await search_tt_standards(query, max_results)
        except Exception as e:
            _log.error(f"团体标准搜索失败 '{query}': {type(e).__name__}: {e}")
            raise HTTPException(status_code=500, detail=f"团体标准搜索失败: {e}")
        # TT 结果已含 detail_id，downloadable 判断：非付费即可下载
        results = [{
            "code": s.get('code', ''),
            "name": s.get('name', ''),
            "tid": 'BV_TT',
            "pid": s.get('detail_id', ''),
            "status": s.get('status', ''),
            "publishDate": s.get('publishDate', ''),
            "actDate": '',
            "downloadable": not s.get('sellable', False),
            "download_msg": '付费标准' if s.get('sellable') else '支持下载',
            "localExists": make_filename(s.get('code', ''), s.get('name', '')) in existing,
            "detailUrl": f"https://www.ttbz.org.cn/StandardManage/Detail/{s.get('detail_id', '')}/",
            "group_name": s.get('group_name', ''),
        } for s in standards]
        local_count = sum(1 for r in results if r['localExists'])
        _log.info(f"团体标准搜索 '{query}': 站点 {len(standards)} 条，展示 {len(results)} 条，"
                  f"本地已有 {local_count} 条")
        return {"success": True, "total": len(results), "results": results}

    try:
        standards, total = await loop.run_in_executor(
            None, fetch_stdpage_search, query, 1, std_type)
    except Exception as e:
        _log.error(f"搜索失败 '{query}'（{std_type}）: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"搜索失败: {e}")

    results = []
    for s in standards[:max_results]:
        can_dl, dl_msg = await loop.run_in_executor(
            None, check_downloadable, s['tid'])
        results.append({
            "code": s.get('code', ''),
            "name": s.get('name', ''),
            "tid": s.get('tid', ''),
            "pid": s.get('pid', ''),
            "status": s.get('status', ''),
            "publishDate": s.get('publishDate', ''),
            "actDate": s.get('actDate', ''),
            "downloadable": can_dl,
            "download_msg": dl_msg,
            "localExists": make_filename(s.get('code', ''), s.get('name', '')) in existing,
            "detailUrl": f"{DETAIL_URL}?id={s.get('tid', '')}",
        })

    local_count = sum(1 for r in results if r['localExists'])
    _log.info(f"搜索 '{query}'（{std_type}）: 站点共 {total} 条，解析 {len(standards)} 条，"
              f"展示 {len(results)} 条，本地已有 {local_count} 条")
    return {"success": True, "total": total, "results": results}


@router.post("/api/search/batch")
async def search_batch_api(request: Request):
    """批量搜索下载：每行一个关键词/标准号，逐个搜索去重后自动下载。

    body: {
        queries: ["关键词1", "标准号1", ...],   # 必填，最多 1000 行
        std_type: "全部" | "国家标准" | ...,    # 默认 "全部"
        per_query_max: 10,                      # 关键词行最大结果数，默认 10
        exact_match: true,                      # 标准号行精确匹配（默认开启）
        skip_obsolete: true,                    # 关键词命中的废止版本默认跳过
        auto_download: true                     # 搜索完成后自动下载，默认 true
    }

    返回 task_id，通过 SSE 推送进度：
      0-50%   搜索阶段（"搜索中 N/M: 关键词"）
      50-100% 下载阶段（复用 _download_selected_items）
    """
    body = await request.json()
    queries = body.get("queries") or []
    std_type = body.get("std_type", "全部")
    try:
        per_query_max = int(body.get("per_query_max", 10))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="per_query_max 必须是整数")
    exact_match = bool(body.get("exact_match", True))
    skip_obsolete = bool(body.get("skip_obsolete", True))
    auto_download = bool(body.get("auto_download", True))

    # 规范化 queries：字符串可按行拆分
    flat = []
    for q in queries:
        if isinstance(q, str):
            for line in q.splitlines():
                line = line.strip()
                if line and not line.startswith('#'):
                    flat.append(line)
    queries, duplicate_queries = _dedupe_queries(flat)

    if std_type not in _SEARCH_STD_TYPES:
        raise HTTPException(status_code=400, detail=f"不支持的标准类型: {std_type}")
    if not queries:
        raise HTTPException(status_code=400, detail="未提供搜索关键词")
    if len(queries) > 1000:
        raise HTTPException(status_code=400, detail=f"单次批量搜索最多 1000 个关键词（当前 {len(queries)} 个）")
    if per_query_max < 1 or per_query_max > 100:
        raise HTTPException(status_code=400, detail="per_query_max 取值范围 1-100")
    if duplicate_queries:
        _log.info(f"批量搜索输入去重: 忽略 {duplicate_queries} 个重复行")

    task_id = f"search_batch_{int(time.time())}"
    task_manager.create(task_id, {
        "task_id": task_id,
        "status": "running",
        "progress": 0,
        "message": f"批量搜索 {len(queries)} 个关键词" +
                   (f"（已忽略 {duplicate_queries} 个重复行）" if duplicate_queries else ""),
        "stats": {"downloaded": 0, "success": 0, "failed": 0, "skipped": 0, "searched": 0},
        "start_time": time.time(),
        "std_type": "search_batch",
        "std_items": [],
    })

    async def batch_task():
        from app.scanner.utils import compute_download_stats, format_download_summary
        from app.helpers import format_duration
        started = time.time()
        try:
            _log.info(f"任务 {task_id}: 批量搜索 {len(queries)} 个关键词"
                      f"（类型={std_type} 每词上限={per_query_max} 精确匹配={exact_match} "
                      f"跳过废止={skip_obsolete} 自动下载={auto_download}）")
            all_items, obsolete_skipped, diag = await _batch_search_dedup(
                task_id, queries, std_type, per_query_max, exact_match, skip_obsolete)
            obsolete_note = f"（搜索阶段跳过废止 {obsolete_skipped} 条）" if obsolete_skipped else ''
            download_only = bool((task_manager.get(task_id) or {}).get('_download_only'))

            # 暂停时等待用户继续，避免直接进入下载阶段或提前标记完成
            if not await _wait_if_running(task_id):
                return

            if not all_items:
                msg = f"搜索完成，未找到可下载的标准｜{diag.brief()}" + obsolete_note
                update_task_status(task_id, progress=100, status="completed",
                                 message=msg, end_time=time.time())
                _log.info(f"任务 {task_id}: 搜索完成（无结果），耗时 {format_duration(time.time() - started)}，"
                          f"{diag.overview()}")
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
                    update_task_status(task_id, progress=100, status="completed",
                                     message=msg, end_time=time.time())
                    return
                prefix = '已结束搜索，开始下载' if download_only else '搜索完成，开始下载'
                update_task_status(task_id, progress=50,
                                 message=f"{prefix} {pending} 个标准{existing_note}（{diag.brief()}）...")
                await _download_selected_items(task_id, all_items, progress_base=50)
                if not await _wait_if_running(task_id):
                    return
                stats = compute_download_stats(all_items)
                update_task_status(task_id, progress=100, status="completed",
                                 message=format_download_summary(stats) + f"｜{diag.brief()}" + obsolete_note,
                                 end_time=time.time())
            else:
                # 仅搜索：把结果存入 std_items 供前端查看
                msg = (f"搜索完成，候选 {len(all_items)} 条，待下载 {pending}{existing_note}"
                       f"｜{diag.brief()}{obsolete_note}")
                task_manager.update(task_id, std_items=all_items, progress=100,
                                  status="completed", message=msg,
                                  end_time=time.time())
            _log.info(f"任务 {task_id}: 完成，耗时 {format_duration(time.time() - started)}，"
                      f"{diag.overview()}")
        except Exception as e:
            _log.error(f"任务 {task_id} 异常: {e}", exc_info=True)
            task_manager.update(task_id, status="failed", message=str(e), end_time=time.time())

    _launch_task(batch_task(), f"batch-{task_id}")
    return {"success": True, "task_id": task_id, "queries_count": len(queries),
            "duplicates_removed": duplicate_queries}


async def _wait_if_running(task_id):
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


def _is_download_only(task_id):
    """批量搜索任务是否被要求"结束搜索、直接下载已有结果"。"""
    task = task_manager.get(task_id)
    return bool(task and task.get('_download_only'))


async def _sleep_with_pause(task_id, seconds):
    """可中断延迟：暂停时立即挂起，任务删除/结束时返回 False。"""
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        if not await _wait_if_running(task_id):
            return False
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return True
        await asyncio.sleep(min(0.2, remaining))


async def _batch_search_dedup(task_id, queries, std_type, per_query_max,
                              exact_match=True, skip_obsolete=True):
    """批量搜索并去重，返回 (候选 items, 搜索阶段跳过的废止条数, 诊断记录器)。

    每搜完一个关键词就把已匹配结果和诊断信息写回任务，前端可实时看到
    "这个查询站点返回几条、为什么没有新增"，而不是只看到一个 +0。

    exact_match=True 时按查询意图过滤：标准号行只保留编号匹配的结果
    （未指定年份时自动排除废止版本），关键词行仍按 per_query_max 截断。

    skip_obsolete=True 时，关键词命中的废止版本直接跳过（标准号行不受影响，
    用户明确查询某个废止编号时仍然保留）。

    去重 key：GB 用 pid；HB/DB 用 code+name（pid 可能跨类型相同）；
    TT（团体标准）用 detail_id。
    """
    from app.scanner import fetch_stdpage_search, check_downloadable, search_tt_standards
    from app.scanner.search import filter_standard_results, parse_standard_code
    from app.scanner.search_diag import SearchBatchDiag
    from app.scanner.utils import compute_download_stats, make_filename
    from app.dedup import get_existing_files
    from config.settings import DETAIL_URL, get_delay

    seen = set()
    all_items = []
    obsolete_skipped = 0
    total = len(queries)
    loop = asyncio.get_running_loop()
    is_tt = (std_type == '团体标准')
    # 搜索阶段先做本地去重：已有文件的条目直接标记，后续不再尝试下载
    existing = await loop.run_in_executor(None, get_existing_files)
    diag = SearchBatchDiag(total)

    for i, q in enumerate(queries):
        # 暂停时挂起；任务被删除/结束时中止搜索
        if not await _wait_if_running(task_id):
            _log.info(f"任务 {task_id}: 搜索被中止于 {i}/{total}")
            break
        # 用户点击"下载已有结果"时立即结束剩余搜索
        if _is_download_only(task_id):
            _log.info(f"任务 {task_id}: 收到下载指令，结束剩余 {total - i} 个关键词搜索")
            break
        update_task_status(task_id,
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
            if not await _wait_if_running(task_id):
                return all_items, obsolete_skipped, diag
            if _is_download_only(task_id):
                return all_items, obsolete_skipped, diag
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

        # 实时回写：候选列表 + 统计 + 搜索诊断，前端展开详情即可看到
        update_task_status(task_id,
                           std_items=all_items,
                           progress=int(50 * (i + 1) / total) if total else 50,
                           message=msg,
                           stats={**compute_download_stats(all_items), 'searched': diag.done},
                           search_diag=diag.to_dict(),
                           persist_std_items=False)
        # 保护搜索站点（可被暂停/删除中断）
        if not await _sleep_with_pause(task_id, get_delay()):
            break

    _log.info(f"任务 {task_id}: 搜索阶段结束，{diag.overview()}，"
              f"废止跳过 {obsolete_skipped} 条")
    return all_items, obsolete_skipped, diag


@router.post("/api/search/download")
async def search_download_selected_api(request: Request):
    from app.scanner.search_download import (
        FINAL_STATUSES, mark_obsolete_skipped, mark_existing_skipped,
    )
    from app.scanner.utils import compute_download_stats, format_download_summary

    body = await request.json()
    items = body.get("items", [])
    # 一键下载全部默认跳过废止；前端显式下载选中时传 false
    skip_obsolete = bool(body.get("skip_obsolete", True))

    if not items:
        raise HTTPException(status_code=400, detail="未选择任何标准")

    skipped_obsolete = mark_obsolete_skipped(items) if skip_obsolete else 0
    loop = asyncio.get_running_loop()
    skipped_existing = await loop.run_in_executor(None, mark_existing_skipped, items)
    pending_count = sum(1 for it in items if it.get('dlStatus') not in FINAL_STATUSES)

    skip_notes = ''
    if skipped_obsolete:
        skip_notes += f"，跳过废止 {skipped_obsolete}"
    if skipped_existing:
        skip_notes += f"，本地已有 {skipped_existing}"

    if pending_count == 0:
        return {"success": True, "task_id": None, "count": 0,
                "skipped_obsolete": skipped_obsolete,
                "skipped_existing": skipped_existing,
                "message": "没有需要下载的标准" + (f"（{skip_notes.lstrip('，')}）" if skip_notes else '')}

    task_id = f"search_{int(time.time())}"
    task_manager.create(task_id, {
        "task_id": task_id,
        "status": "running",
        "progress": 0,
        "message": f"下载中 {pending_count} 个标准{skip_notes}",
        "stats": compute_download_stats(items),
        "start_time": time.time(),
        "std_type": "search",
        "std_items": []
    })

    async def download_task():
        try:
            _log.info(f"任务 {task_id}: 下载选中标准 {pending_count} 个（共 {len(items)} 条）")
            await _download_selected_items(task_id, items, progress_base=0)

            update_task_status(task_id, progress=100, status="completed",
                             message=format_download_summary(compute_download_stats(items)))
            task_manager.update(task_id, end_time=time.time())
            _log.info(f"任务 {task_id}: 完成")
        except Exception as e:
            _log.error(f"任务 {task_id} 异常: {e}")
            task_manager.update(task_id, status="failed", message=str(e), end_time=time.time())

    _launch_task(download_task(), f"download-{task_id}")
    return {"success": True, "task_id": task_id, "count": pending_count,
            "skipped_obsolete": skipped_obsolete, "skipped_existing": skipped_existing}


async def _download_selected_items(task_id, items, progress_base=0):
    """下载搜索结果并回写每条标准的下载状态。

    progress_base 控制进度条起始百分比：
    - 0  用于 /api/search/download 与任务一键下载（0-99%）
    - 50 用于 /api/search/batch（50-99%，前半段是搜索）

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
    update_task_status(task_id, std_items=items,
                       stats=compute_download_stats(items),
                       persist_std_items=False)
    try:
        for i, item in enumerate(items):
            if not await _wait_if_running(task_id):
                _log.info(f"任务 {task_id}: 下载被中止")
                return

            code = item.get('code') or item.get('stdCode', '')
            name = item.get('name') or item.get('stdName', '')
            item_label = f"{code} {name}".strip()
            is_done = item.get('dlStatus') in FINAL_STATUSES
            dl_pct = progress_base + int(span * i / total) if total else 99
            update_task_status(task_id, progress=dl_pct,
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
                if not await _wait_if_running(task_id):
                    return
            if not ok:
                _log.info(f"  [FAIL] {status}" + (f": {reason}" if reason else ""))
                if not await _sleep_with_pause(task_id, get_delay()):
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


async def download_one_search_item(item, output_dir, existing):
    """供任务单条下载/重试复用：下载一条搜索结果并回写 dlStatus。"""
    from app.scanner.search_download import SearchDownloadSession
    session = SearchDownloadSession(output_dir, existing=existing)
    try:
        return await session.download_one(item, output_dir)
    finally:
        await session.close()


# 定时任务管理 API