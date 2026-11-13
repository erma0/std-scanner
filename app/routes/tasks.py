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
    """继续任务"""
    if not task_manager.exists(task_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    if not task_manager.resume(task_id):
        raise HTTPException(status_code=400, detail="只能继续已暂停的任务")
    return {"success": True, "task_id": task_id}


@router.post("/api/task/{task_id}/retry")
async def retry_task_api(task_id: str):
    """重试任务（统一走 run_scan_pipeline）"""
    task = task_manager.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    std_type = task.get('std_type')

    if std_type == 'search':
        raise HTTPException(status_code=400, detail="搜索下载任务不支持直接重试，请重新提交搜索")

    keyword_group = task.get('keyword_group', '安全生产')
    max_results = task.get('max_results', 500)
    incr = task.get('incr', False)
    scan_only = task.get('scan_only', False)

    if std_type in ('gb', 'hb', 'db', 'tt', 'mem'):
        config = {
            'max_results': max_results,
            'incr': incr,
            'keyword_group': keyword_group,
            'scan_only': scan_only,
            'industries': task.get('industries'),
            'provinces': task.get('provinces'),
            'cnl1_codes': task.get('cnl1_codes'),
            'source': task.get('source', 'bz') if std_type == 'mem' else None,
        }
        # 移除 None 值，避免污染 config
        config = {k: v for k, v in config.items() if v is not None}
        result = _create_scan_task(
            scan_type=std_type,
            task_id_prefix=std_type,
            config=config,
        )
    elif std_type == 'all':
        # all 类型已废弃，改为创建独立任务（保留原 scan_types）
        scan_types = task.get('scan_types', ['gb', 'hb', 'db'])
        task_ids, scan_fn = create_combined_scan_tasks(
            scan_types=scan_types,
            max_results=max_results,
            incr=incr,
            keyword_group=keyword_group,
            scan_only=scan_only,
            hb_config={'industries': task.get('industries')},
            db_config={'provinces': task.get('provinces')},
            tt_config={'cnl1_codes': task.get('cnl1_codes')},
        )
        _launch_task(scan_fn(), "retry-combined")
        return {"success": True, "new_task_ids": task_ids}
    else:
        raise HTTPException(status_code=400, detail=f"不支持的任务类型: {std_type}")

    return {"success": True, "new_task_id": result['task_id']}


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
