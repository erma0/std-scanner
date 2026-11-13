"""API routes — Tasks"""
from fastapi import APIRouter, HTTPException, Request
from typing import Optional
import time
import logging

from .state import task_manager
from ._utils import launch_task as _launch_task, create_combined_scan_tasks
from .scan import _create_scan_task

# 以下模块导入耗时较长（scanner/captcha/dedup 链），延迟到首次使用时：
# - app.keywords, app.scanner.utils, app.scanner.hb_scan, app.scanner.download
# - app.scanner.download_helpers, app.scanner.gb_scan, app.dedup
# - config.settings (get_output_dir, get_delay)

_log = logging.getLogger('std_scraper')

router = APIRouter(prefix="", tags=["Tasks"])


def _scan_config_from_task(task, std_type):
    """从历史任务还原扫描配置（继续/重新开始共用），避免丢失筛选条件。"""
    config = {
        'max_results': task.get('max_results', 500),
        'incr': task.get('incr', False),
        'keyword_group': task.get('keyword_group', '安全生产'),
        'scan_only': task.get('scan_only', False),
        'std_state': task.get('std_state', '现行'),
        'industries': task.get('industries'),
        'provinces': task.get('provinces'),
        'cnl1_codes': task.get('cnl1_codes'),
        'allow_preview': task.get('allow_preview'),
    }
    if std_type == 'mem':
        if task.get('mem_sites'):
            config['sites'] = task.get('mem_sites')
        else:
            config['site'] = task.get('mem_site', 'mem')
            config['source'] = task.get('mem_source', 'bz')
    # 移除 None 值，避免污染 config
    return {k: v for k, v in config.items() if v is not None}


async def _resume_interrupted_task(task):
    """从已持久化的进度继续中断/失败的任务，返回 {mode, ...}。"""
    task_id = task.get('task_id')
    std_type = task.get('std_type')

    if std_type == 'search_batch':
        from app.search_batch_service import run_batch_search_pipeline
        queries = task.get('search_queries') or []
        if not queries:
            raise HTTPException(status_code=400,
                                detail="任务未保存搜索关键词，无法继续；请重新发起批量搜索")
        done = int(task.get('search_done_count') or 0)
        remaining = max(0, len(queries) - done)
        task_manager.update(
            task_id, status='running', end_time=None, _download_only=False,
            search_phase='searching' if remaining else 'downloading',
            progress=int(50 * done / len(queries)) if queries else 0,
            message=f'继续批量搜索（剩余 {remaining} 个关键词）' if remaining else '继续下载搜索结果...')
        _launch_task(run_batch_search_pipeline(task_manager, task_id),
                     f"resume-search-{task_id}")
        return {"mode": "search", "remaining": remaining}

    if std_type == 'search':
        from app.task_service import start_download_all, run_download_all
        result = await start_download_all(task_manager, task, skip_obsolete=True)
        if result.pop('launch', False):
            _launch_task(run_download_all(task_manager, task_id,
                                          result.pop('items'), result.get('count')),
                         f"resume-download-{task_id}")
        return {"mode": "download", **result}

    if std_type in ('gb', 'hb', 'db', 'tt', 'mem'):
        from app.task_service import resume_scan_download
        # 中断/失败在下载阶段：保留扫描结果，只补未完成的下载。
        # 进度 > 40 表示扫描阶段已结束（扫描占 0-40%，下载占 40-100%）。
        in_download = (task.get('dl_progress') or 0) > 0 or (task.get('progress') or 0) > 40
        if (std_type in ('gb', 'hb', 'db') and not task.get('scan_only')
                and in_download and task.get('std_items')):
            task_manager.update(task_id, status='running', end_time=None,
                                message='继续下载...')
            _launch_task(resume_scan_download(task_manager, task_id),
                         f"resume-download-{task_id}")
            return {"mode": "download"}
        # 扫描阶段中断（或 TT/MEM）：同任务重新扫描，已下载文件会被跳过
        _create_scan_task(std_type, std_type,
                          _scan_config_from_task(task, std_type),
                          resume_task_id=task_id)
        return {"mode": "scan"}

    raise HTTPException(status_code=400, detail=f"不支持的任务类型: {std_type}")


def _restart_task(task):
    """按原参数创建全新任务（重新开始），返回接口响应。"""
    std_type = task.get('std_type')

    if std_type in ('gb', 'hb', 'db', 'tt', 'mem'):
        result = _create_scan_task(std_type, std_type,
                                   _scan_config_from_task(task, std_type))
        return {"success": True, "new_task_id": result['task_id']}

    if std_type == 'all':
        # all 类型已废弃，改为创建独立任务（保留原 scan_types）
        scan_types = task.get('scan_types', ['gb', 'hb', 'db'])
        task_ids, scan_fn = create_combined_scan_tasks(
            scan_types=scan_types,
            max_results=task.get('max_results', 500),
            incr=task.get('incr', False),
            keyword_group=task.get('keyword_group', '安全生产'),
            scan_only=task.get('scan_only', False),
            std_state=task.get('std_state', '现行'),
            hb_config={'industries': task.get('industries')},
            db_config={'provinces': task.get('provinces')},
            tt_config={'cnl1_codes': task.get('cnl1_codes')},
        )
        _launch_task(scan_fn(), "restart-combined")
        return {"success": True, "new_task_ids": task_ids}

    if std_type == 'search_batch':
        from app.routes.search import _create_batch_search_task
        queries = list(task.get('search_queries') or [])
        if not queries:
            raise HTTPException(status_code=400,
                                detail="任务未保存搜索关键词，无法重新开始；请重新发起批量搜索")
        new_id = _create_batch_search_task(
            queries,
            task.get('search_std_type') or '全部',
            int(task.get('search_per_query_max') or 10),
            bool(task.get('search_exact_match', True)),
            bool(task.get('search_skip_obsolete', True)),
            bool(task.get('search_auto_download', True)),
        )
        return {"success": True, "new_task_id": new_id}

    if std_type == 'search':
        from app.task_service import create_download_restart_task, run_download_all
        new_id, items = create_download_restart_task(task_manager, task)
        if not new_id:
            raise HTTPException(status_code=400, detail="任务中没有可下载的标准")
        _launch_task(run_download_all(task_manager, new_id, items, len(items)),
                     f"restart-download-{new_id}")
        return {"success": True, "new_task_id": new_id}

    raise HTTPException(status_code=400, detail=f"不支持的任务类型: {std_type}")


@router.get("/api/task/{task_id}")
async def get_task_status(task_id: str):
    """获取任务状态"""
    task = task_manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@router.get("/api/tasks")
async def get_all_tasks(status: Optional[str] = None):
    """获取所有任务（可按状态筛选）。

    为每个任务动态计算 duration 字段（运行中任务用当前时间，暂停状态不计入新增时长）。
    """
    tasks = task_manager.get_all(status_filter=status)
    now = time.time()
    for t in tasks:
        start_time = t.get('start_time')
        if not start_time:
            continue
        end_time = t.get('end_time')
        if end_time is None and t.get('status') == 'running':
            end_time = now
        if not end_time:
            continue
        paused_dur = t.get('paused_duration', 0) or 0
        if t.get('status') == 'paused':
            paused_at = t.get('paused_at')
            if paused_at:
                paused_dur += max(0, now - paused_at)
        t['duration'] = max(0, end_time - start_time - paused_dur)
    return tasks


@router.delete("/api/task/{task_id}")
async def delete_task_api(task_id: str):
    """删除任务"""
    if task_manager.delete(task_id):
        _log.info(f"任务已删除: {task_id}")
        return {"success": True}
    raise HTTPException(status_code=404, detail="任务不存在")


@router.delete("/api/tasks")
async def delete_all_tasks_api():
    """删除所有任务"""
    task_manager.delete_all()
    _log.info("所有任务已清除")
    return {"success": True}


@router.post("/api/task/{task_id}/pause")
async def pause_task_api(task_id: str):
    """暂停任务"""
    if not task_manager.exists(task_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    if not task_manager.pause(task_id):
        raise HTTPException(status_code=400, detail="只能暂停运行中的任务")
    return {"success": True, "task_id": task_id}


@router.post("/api/task/{task_id}/resume")
async def resume_task_api(task_id: str):
    """继续任务。

    - paused：唤醒内存中的后台协程（原行为）
    - interrupted / failed：从已保存进度续跑
        * 扫描任务：中断在下载阶段则只补下载，否则重新扫描（同任务）
        * 批量搜索：从 search_done_count 继续搜索剩余关键词，再下载
        * 下载任务：只下载尚未完成的条目
    """
    task = task_manager.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    status = task.get('status')
    if status == 'paused':
        if not task_manager.resume(task_id):
            raise HTTPException(status_code=400, detail="只能继续已暂停的任务")
        return {"success": True, "task_id": task_id, "mode": "resume"}

    if status not in ('interrupted', 'failed'):
        raise HTTPException(status_code=400,
                            detail="仅已暂停、已中断或失败的任务支持继续")
    result = await _resume_interrupted_task(task)
    return {"success": True, "task_id": task_id, **result}


@router.post("/api/task/{task_id}/retry")
async def retry_task_api(task_id: str):
    """重试任务（等同于重新开始，创建新任务）。"""
    task = task_manager.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.get('status') in ('running', 'pending'):
        raise HTTPException(status_code=400, detail="任务正在运行，请等待完成或先暂停")
    _stop_paused_task(task)
    return _restart_task(task)


def _stop_paused_task(task):
    """重新开始前终止暂停任务的协程：标记为中断并保留历史记录。

    后台协程会在下一个暂停检查点看到非 running/paused 状态并退出。
    """
    if task.get('status') == 'paused':
        task_manager.update(task['task_id'], status='interrupted',
                            end_time=time.time(), currentItem='',
                            message='已被重新开始替代')


@router.post("/api/task/{task_id}/restart")
async def restart_task_api(task_id: str):
    """重新开始任务：沿用原参数创建全新任务，原任务记录保留。

    暂停中的任务也可重新开始：旧任务会被标记为已中断并停止后台协程。
    与"继续"的区别：继续在同一任务内接续进度，重新开始则清空进度从头执行。
    """
    task = task_manager.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.get('status') in ('running', 'pending'):
        raise HTTPException(status_code=400,
                            detail="任务正在运行，请先暂停或等待完成后再重新开始")
    _stop_paused_task(task)
    return _restart_task(task)


@router.get("/api/task/{task_id}/detail")
async def get_task_detail_api(task_id: str):
    """获取任务详情（包含完整信息）"""
    task = task_manager.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    duration = None
    if task.get('start_time'):
        end_time = task.get('end_time')
        if end_time is None and task.get('status') == 'running':
            end_time = time.time()
        if end_time:
            paused_dur = task.get('paused_duration', 0) or 0
            # 暂停状态下不计入暂停期间的新增时长
            if task.get('status') == 'paused':
                paused_at = task.get('paused_at')
                if paused_at:
                    paused_dur += max(0, time.time() - paused_at)
            duration = max(0, end_time - task['start_time'] - paused_dur)

    result = dict(task)
    result['duration'] = duration
    return result


@router.post("/api/task/{task_id}/priority")
async def set_task_priority(task_id: str, request: Request):
    """设置任务优先级"""
    if not task_manager.exists(task_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    body = await request.json()
    priority = body.get("priority", 0)
    if not isinstance(priority, int) or priority < 0:
        raise HTTPException(status_code=400, detail="优先级必须是非负整数")
    if task_manager.bump_priority(task_id, delta=priority - task_manager.get(task_id).get('priority', 0)):
        return {"success": True, "task_id": task_id, "priority": priority}
    raise HTTPException(status_code=400, detail="设置优先级失败")


@router.post("/api/task/{task_id}/retry-item/{item_index}")
async def retry_single_item(task_id: str, item_index: int):
    """重试下载单条标准（不重新扫描，仅重新下载该条）"""
    from app.task_service import retry_single_item as _retry_single_item

    task = task_manager.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.get('status') == 'running':
        raise HTTPException(status_code=400, detail="任务正在运行，请等待完成后再重试单条")

    items = task.get('std_items', [])
    if item_index < 0 or item_index >= len(items):
        raise HTTPException(status_code=400, detail=f"索引 {item_index} 超出范围 (0-{len(items)-1})")

    return await _retry_single_item(
        task_manager, task_id, items[item_index], item_index, task.get('std_type'))


@router.post("/api/task/{task_id}/download-all")
async def download_all_task_items(task_id: str, skip_obsolete: bool = True):
    """一键下载搜索结果任务中所有尚未完成的标准（不重新搜索）。

    - 已完成/失败/中断的任务：新起下载协程
    - 已暂停的任务：继续原后台任务，并让批量搜索直接进入下载阶段
    - skip_obsolete=True 时先把废止标准标记为 skipped_obsolete
    - 本地已存在的标准先标记 skipped_existing，不进入下载队列
    """
    from app.task_service import TaskStateError, start_download_all, run_download_all

    task = task_manager.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    status = task.get('status')
    if status in ('running', 'pending'):
        raise HTTPException(status_code=400, detail="任务正在运行，请等待完成后再下载")
    if task.get('std_type') not in ('search', 'search_batch'):
        raise HTTPException(status_code=400, detail="仅搜索结果任务支持一键下载")
    if not (task.get('std_items') or []):
        raise HTTPException(status_code=400, detail="任务中没有可下载的标准")

    try:
        result = await start_download_all(task_manager, task, skip_obsolete)
    except TaskStateError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if result.pop('launch', False):
        _launch_task(run_download_all(task_manager, task_id,
                                      result.pop('items'), result.get('count')),
                     f"download-all-{task_id}")
    return result


@router.post("/api/task/{task_id}/retry-failed")
async def retry_all_failed(task_id: str):
    """批量重试所有下载失败的标准"""
    from app.task_service import retry_failed_items

    task = task_manager.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.get('status') == 'running':
        raise HTTPException(status_code=400, detail="任务正在运行，请等待完成后再重试")

    return await retry_failed_items(
        task_manager, task_id, task.get('std_items', []), task.get('std_type'))
