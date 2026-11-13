"""API routes — Search"""
from fastapi import APIRouter, HTTPException, Request
import time
import asyncio
import logging

from ._utils import launch_task as _launch_task, update_task_status
from .state import task_manager
from app.search_batch_service import (
    batch_search_dedup as _batch_search_dedup,
    download_selected_items as _download_selected_items,
    wait_if_running as _wait_if_running,
    is_download_only as _is_download_only,
)

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


async def _notify_search_done(task_id, message, stats):
    """搜索/下载任务完成后按配置渠道发送通知（线程执行，避免阻塞事件循环）。"""
    try:
        from app.notifier import get_notification_service
        from app.helpers import format_duration
        ns = get_notification_service()
        if not ns.notifiers:
            return
        task = task_manager.get(task_id) or {}
        report = dict(stats or {})
        report['time'] = time.strftime('%Y-%m-%d %H:%M:%S')
        report['message'] = message
        if task.get('start_time'):
            report['duration'] = format_duration(
                (task.get('end_time') or time.time()) - task['start_time'])
        await asyncio.to_thread(ns.send_report, report, task_id)
    except Exception as e:
        _log.debug(f"搜索任务通知发送失败: {e}")


async def _notify_search_failed(task_id, error):
    """搜索任务异常时的错误通知。"""
    try:
        from app.notifier import get_notification_service
        ns = get_notification_service()
        if not ns.notifiers:
            return
        await asyncio.to_thread(
            ns.send_message, '搜索任务失败',
            f'任务 {task_id} 执行失败\n\n错误信息: {str(error)[:300]}', task_id)
    except Exception as e:
        _log.debug(f"搜索任务失败通知发送失败: {e}")


def _create_batch_search_task(queries, std_type, per_query_max, exact_match,
                              skip_obsolete, auto_download,
                              duplicate_queries=0, parent_task_id=None):
    """创建并启动批量搜索任务，返回 task_id。

    搜索参数会写入任务字段，供"重试无新增查询"复用，避免前端重复传参。
    """
    task_id = f"search_batch_{int(time.time() * 1000)}"
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
        "search_std_type": std_type,
        "search_per_query_max": per_query_max,
        "search_exact_match": exact_match,
        "search_skip_obsolete": skip_obsolete,
        "search_auto_download": auto_download,
        "parent_task_id": parent_task_id,
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
                task_manager, task_id, queries, std_type, per_query_max, exact_match, skip_obsolete)
            obsolete_note = f"（搜索阶段跳过废止 {obsolete_skipped} 条）" if obsolete_skipped else ''
            download_only = bool((task_manager.get(task_id) or {}).get('_download_only'))

            # 暂停时等待用户继续，避免直接进入下载阶段或提前标记完成
            if not await _wait_if_running(task_manager, task_id):
                return

            if not all_items:
                msg = f"搜索完成，未找到可下载的标准｜{diag.brief()}" + obsolete_note
                update_task_status(task_id, progress=100, status="completed",
                                 message=msg, end_time=time.time())
                _log.info(f"任务 {task_id}: 搜索完成（无结果），耗时 {format_duration(time.time() - started)}，"
                          f"{diag.overview()}")
                await _notify_search_done(task_id, msg, compute_download_stats(all_items))
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
                    await _notify_search_done(task_id, msg, stats)
                    return
                prefix = '已结束搜索，开始下载' if download_only else '搜索完成，开始下载'
                update_task_status(task_id, progress=50,
                                 message=f"{prefix} {pending} 个标准{existing_note}（{diag.brief()}）...")
                await _download_selected_items(task_manager, task_id, all_items, progress_base=50)
                if not await _wait_if_running(task_manager, task_id):
                    return
                stats = compute_download_stats(all_items)
                final_msg = format_download_summary(stats) + f"｜{diag.brief()}" + obsolete_note
                update_task_status(task_id, progress=100, status="completed",
                                 message=final_msg, end_time=time.time())
                await _notify_search_done(task_id, final_msg, stats)
            else:
                # 仅搜索：把结果存入 std_items 供前端查看
                msg = (f"搜索完成，候选 {len(all_items)} 条，待下载 {pending}{existing_note}"
                       f"｜{diag.brief()}{obsolete_note}")
                task_manager.update(task_id, std_items=all_items, progress=100,
                                  status="completed", message=msg,
                                  end_time=time.time())
                await _notify_search_done(task_id, msg, stats)
            _log.info(f"任务 {task_id}: 完成，耗时 {format_duration(time.time() - started)}，"
                      f"{diag.overview()}")
        except Exception as e:
            _log.error(f"任务 {task_id} 异常: {e}", exc_info=True)
            task_manager.update(task_id, status="failed", message=str(e), end_time=time.time())
            await _notify_search_failed(task_id, e)

    _launch_task(batch_task(), f"batch-{task_id}")
    return task_id


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

    task_id = _create_batch_search_task(
        queries, std_type, per_query_max, exact_match,
        skip_obsolete, auto_download, duplicate_queries)
    return {"success": True, "task_id": task_id, "queries_count": len(queries),
            "duplicates_removed": duplicate_queries}


@router.post("/api/search/task/{task_id}/retry-misses")
async def retry_search_misses_api(task_id: str):
    """重试批量搜索中最近 0 新增的查询（站点无结果/编号不匹配/请求异常）。

    诊断样本由 SearchBatchDiag 记录（最多 50 条），沿用原任务的搜索参数创建新任务。
    """
    task = task_manager.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.get('std_type') != 'search_batch':
        raise HTTPException(status_code=400, detail="仅批量搜索任务支持重试无新增查询")
    if task.get('status') in ('running', 'pending'):
        raise HTTPException(status_code=400, detail="任务正在运行，请暂停或等待完成后再重试")

    samples = (task.get('search_diag') or {}).get('zero_samples') or []
    queries, seen = [], set()
    for sample in samples:
        q = str(sample.get('query') or '').strip()
        key = ' '.join(q.upper().split())
        if q and key not in seen:
            seen.add(key)
            queries.append(q)
    if not queries:
        raise HTTPException(status_code=400, detail="没有可重试的查询（仅记录最近 50 个 0 新增查询）")

    new_id = _create_batch_search_task(
        queries,
        task.get('search_std_type') or '全部',
        int(task.get('search_per_query_max') or 10),
        bool(task.get('search_exact_match', True)),
        bool(task.get('search_skip_obsolete', True)),
        bool(task.get('search_auto_download', True)),
        parent_task_id=task_id,
    )
    _log.info(f"任务 {task_id}: 已创建无新增查询重试任务 {new_id}（{len(queries)} 个查询）")
    return {"success": True, "task_id": new_id, "count": len(queries)}


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
            await _download_selected_items(task_manager, task_id, items, progress_base=0)

            stats = compute_download_stats(items)
            final_msg = format_download_summary(stats)
            update_task_status(task_id, progress=100, status="completed",
                             message=final_msg)
            task_manager.update(task_id, end_time=time.time())
            _log.info(f"任务 {task_id}: 完成")
            await _notify_search_done(task_id, final_msg, stats)
        except Exception as e:
            _log.error(f"任务 {task_id} 异常: {e}")
            task_manager.update(task_id, status="failed", message=str(e), end_time=time.time())
            await _notify_search_failed(task_id, e)

    _launch_task(download_task(), f"download-{task_id}")
    return {"success": True, "task_id": task_id, "count": pending_count,
            "skipped_obsolete": skipped_obsolete, "skipped_existing": skipped_existing}


async def download_one_search_item(item, output_dir, existing):
    """供任务单条下载/重试复用：下载一条搜索结果并回写 dlStatus。"""
    from app.scanner.search_download import SearchDownloadSession
    session = SearchDownloadSession(output_dir, existing=existing)
    try:
        return await session.download_one(item, output_dir)
    finally:
        await session.close()


# 定时任务管理 API