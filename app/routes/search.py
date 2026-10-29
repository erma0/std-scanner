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


router = APIRouter(prefix="", tags=["Search"])

@router.post("/api/search/query")
async def search_query_api(request: Request):
    from app.scanner import (
        fetch_stdpage_search, check_downloadable,
        search_tt_standards,
    )
    from config.settings import DETAIL_URL

    body = await request.json()
    query = body.get("query", "")
    std_type = body.get("std_type", "全部")
    max_results = body.get("max_results", 20)

    if not query:
        raise HTTPException(status_code=400, detail="搜索关键词不能为空")

    # 团体标准走独立 API 搜索（ttbz.org.cn，无 WAF，httpx 直接调用）
    if std_type == '团体标准':
        try:
            standards = await search_tt_standards(query, max_results)
        except Exception as e:
            _log.error(f"团体标准搜索失败: {e}")
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
            "download_msg": '付费标准' if s.get('sellable') else '可下载',
            "detailUrl": f"https://www.ttbz.org.cn/StandardManage/Detail/{s.get('detail_id', '')}/",
            "group_name": s.get('group_name', ''),
        } for s in standards]
        return {"success": True, "total": len(results), "results": results}

    try:
        loop = asyncio.get_running_loop()
        standards, total = await loop.run_in_executor(
            None, fetch_stdpage_search, query, 1, std_type)
    except Exception as e:
        _log.error(f"搜索失败: {e}")
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
            "detailUrl": f"{DETAIL_URL}?id={s.get('tid', '')}",
        })

    return {"success": True, "total": total, "results": results}


@router.post("/api/search/batch")
async def search_batch_api(request: Request):
    """批量搜索下载：每行一个关键词/标准号，逐个搜索去重后自动下载。

    body: {
        queries: ["关键词1", "标准号1", ...],   # 必填
        std_type: "全部" | "国家标准" | ...,    # 默认 "全部"
        per_query_max: 10,                      # 关键词行最大结果数，默认 10
        exact_match: true,                      # 标准号行精确匹配（默认开启）
        auto_download: true                     # 搜索完成后自动下载，默认 true
    }

    返回 task_id，通过 SSE 推送进度：
      0-50%   搜索阶段（"搜索中 N/M: 关键词"）
      50-100% 下载阶段（复用 _download_selected_items）
    """
    body = await request.json()
    queries = body.get("queries", [])
    std_type = body.get("std_type", "全部")
    per_query_max = int(body.get("per_query_max", 10))
    exact_match = bool(body.get("exact_match", True))
    auto_download = bool(body.get("auto_download", True))

    # 规范化 queries：字符串可按行拆分
    flat = []
    for q in queries:
        if isinstance(q, str):
            for line in q.splitlines():
                line = line.strip()
                if line and not line.startswith('#'):
                    flat.append(line)
    queries = flat

    if not queries:
        raise HTTPException(status_code=400, detail="未提供搜索关键词")
    if per_query_max < 1 or per_query_max > 100:
        raise HTTPException(status_code=400, detail="per_query_max 取值范围 1-100")

    task_id = f"search_batch_{int(time.time())}"
    task_manager.create(task_id, {
        "task_id": task_id,
        "status": "running",
        "progress": 0,
        "message": f"批量搜索 {len(queries)} 个关键词",
        "stats": {"downloaded": 0, "success": 0, "failed": 0, "skipped": 0, "searched": 0},
        "start_time": time.time(),
        "std_type": "search_batch",
        "std_items": [],
    })

    async def batch_task():
        try:
            _log.info(f"任务 {task_id}: 批量搜索 {len(queries)} 个关键词")
            all_items = await _batch_search_dedup(task_id, queries, std_type,
                                                 per_query_max, exact_match)

            # 暂停时等待用户继续，避免直接进入下载阶段或提前标记完成
            if not await _wait_if_running(task_id):
                return

            if not all_items:
                update_task_status(task_id, progress=100, status="completed",
                                 message="搜索完成，未找到可下载的标准",
                                 end_time=time.time())
                return

            _log.info(f"任务 {task_id}: 去重后 {len(all_items)} 个标准，auto_download={auto_download}")
            if auto_download:
                update_task_status(task_id, progress=50,
                                 message=f"搜索完成（{len(all_items)} 个），开始下载...")
                await _download_selected_items(task_id, all_items, progress_base=50)
                if not await _wait_if_running(task_id):
                    return
                update_task_status(task_id, progress=100, status="completed",
                                 message=f"批量任务完成（{len(all_items)} 个标准）",
                                 end_time=time.time())
            else:
                # 仅搜索：把结果存入 std_items 供前端查看
                task_manager.update(task_id, std_items=all_items, progress=100,
                                  status="completed",
                                  message=f"搜索完成，共 {len(all_items)} 个可下载标准",
                                  end_time=time.time())
            _log.info(f"任务 {task_id}: 完成")
        except Exception as e:
            _log.error(f"任务 {task_id} 异常: {e}", exc_info=True)
            task_manager.update(task_id, status="failed", message=str(e), end_time=time.time())

    _launch_task(batch_task(), f"batch-{task_id}")
    return {"success": True, "task_id": task_id, "queries_count": len(queries)}


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


async def _batch_search_dedup(task_id, queries, std_type, per_query_max, exact_match=True):
    """批量搜索并去重，返回可下载的 items 列表。

    每搜完一个关键词就把已匹配结果写回任务，前端可在搜索过程中/暂停时
    看到实时结果，而不是等全部完成。

    exact_match=True 时按查询意图过滤：标准号行只保留编号匹配的结果
    （未指定年份时自动排除废止版本），关键词行仍按 per_query_max 截断。

    去重 key：GB 用 pid；HB/DB 用 code+name（pid 可能跨类型相同）；
    TT（团体标准）用 detail_id。
    """
    from app.scanner import fetch_stdpage_search, check_downloadable, search_tt_standards
    from app.scanner.search import filter_standard_results, parse_standard_code
    from app.scanner.utils import compute_download_stats
    from config.settings import DETAIL_URL, get_delay

    seen = set()
    all_items = []
    total = len(queries)
    loop = asyncio.get_running_loop()
    is_tt = (std_type == '团体标准')

    for i, q in enumerate(queries):
        # 暂停时挂起；任务被删除/结束时中止搜索
        if not await _wait_if_running(task_id):
            _log.info(f"任务 {task_id}: 搜索被中止")
            break
        update_task_status(task_id,
                        progress=int(50 * i / total) if total else 50,
                        message=f"搜索中 {i+1}/{total}: {q[:30]}（已匹配 {len(all_items)} 条）")
        try:
            if is_tt:
                # 团体标准走 httpx 直连 API 搜索
                standards = await search_tt_standards(q, per_query_max)
                total_count = len(standards)
            else:
                standards, total_count = await loop.run_in_executor(
                    None, fetch_stdpage_search, q, 1, std_type)
        except Exception as e:
            _log.warning(f"搜索 '{q}' 失败: {e}")
            continue

        if exact_match:
            standards, is_code_query = filter_standard_results(q, standards)
        else:
            is_code_query = False
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
                        retry_standards, _ = await loop.run_in_executor(
                            None, fetch_stdpage_search, retry_q, 1, std_type)
                    standards, _ = filter_standard_results(q, retry_standards)
                except Exception as e:
                    _log.warning(f"按编号二次搜索 '{retry_q}' 失败: {e}")

        if is_code_query and not standards:
            _log.info(f"  [{i+1}/{total}] '{q[:40]}' 未匹配到对应编号")

        added = 0
        for s in standards:
            # 暂停时立即挂起；任务删除/结束时停止批量搜索
            if not await _wait_if_running(task_id):
                return all_items
            if is_tt:
                # 团体标准结果格式
                tid = 'BV_TT'
                pid = s.get('detail_id', '')
                code = s.get('code', '')
                name = s.get('name', '')
                detail_id = pid
                key = f"BV_TT|{detail_id}"
                if key in seen:
                    continue
                seen.add(key)
                # 付费标准跳过
                if s.get('sellable'):
                    continue
                all_items.append({
                    "code": code,
                    "name": name,
                    "tid": tid,
                    "pid": pid,
                    "status": s.get('status', ''),
                    "publishDate": s.get('publishDate', ''),
                    "actDate": '',
                    "downloadable": True,
                    "download_msg": '可下载',
                    "detailUrl": f"https://www.ttbz.org.cn/StandardManage/Detail/{detail_id}/",
                    "query": q,
                })
                added += 1
                continue

            tid = s.get('tid', '')
            pid = s.get('pid', '')
            code = s.get('code', '')
            name = s.get('name', '')
            # 去重 key：GB 用 pid（30 char hex）；HB/DB tid 是 BV_HB/BV_DB，pid 是 64 位 hash
            key = f"{tid}|{pid}" if pid else f"{tid}|{code}|{name}"
            if key in seen:
                continue
            seen.add(key)

            can_dl, dl_msg = await loop.run_in_executor(None, check_downloadable, tid)
            if not can_dl:
                continue
            all_items.append({
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
            })
            added += 1

        _log.info(f"  [{i+1}/{total}] '{q[:30]}' +{added} (累计 {len(all_items)})")
        # 实时回写已匹配结果：前端展开详情即可看到（下载阶段会再次全量持久化）
        update_task_status(task_id,
                        std_items=all_items,
                        stats={**compute_download_stats(all_items), 'searched': i + 1},
                        persist_std_items=False)
        # 保护搜索站点（可被暂停/删除中断）
        if not await _sleep_with_pause(task_id, get_delay()):
            break

    return all_items


@router.post("/api/search/download")
async def search_download_selected_api(request: Request):
    body = await request.json()
    items = body.get("items", [])

    if not items:
        raise HTTPException(status_code=400, detail="未选择任何标准")

    task_id = f"search_{int(time.time())}"

    task_manager.create(task_id, {
        "task_id": task_id,
        "status": "running",
        "progress": 0,
        "message": f"下载中 {len(items)} 个标准",
        "stats": {"downloaded": 0, "success": 0, "failed": 0, "skipped": 0},
        "start_time": time.time(),
        "std_type": "search",
        "std_items": []
    })

    async def download_task():
        try:
            _log.info(f"任务 {task_id}: 下载选中标准 {len(items)} 个")
            await _download_selected_items(task_id, items, progress_base=0)

            update_task_status(task_id, progress=100, status="completed",
                             message=f"下载完成({len(items)}个标准)")
            task_manager.update(task_id, end_time=time.time())
            _log.info(f"任务 {task_id}: 完成")
        except Exception as e:
            _log.error(f"任务 {task_id} 异常: {e}")
            task_manager.update(task_id, status="failed", message=str(e), end_time=time.time())

    _launch_task(download_task(), f"download-{task_id}")
    return {"success": True, "task_id": task_id}


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

            code = item.get('code', '')
            name = item.get('name', '')
            dl_pct = progress_base + int(span * i / total) if total else 99
            update_task_status(task_id, progress=dl_pct,
                               message=f"[{i+1}/{total}] 处理: {code} {name}")

            if item.get('dlStatus') in FINAL_STATUSES:
                continue

            _log.info(f"[{i+1}/{total}] 处理: {code} {name}")
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
                            stats=compute_download_stats(items))
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