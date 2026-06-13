"""API routes — Tasks"""
from fastapi import APIRouter, HTTPException, Request
from typing import Optional
import time
import asyncio
import os
import logging

from .state import task_manager
from app.helpers import atomic_write
from ._utils import launch_task as _launch_task, create_combined_scan_tasks
from .scan import _create_scan_task
from app.keywords import set_active_group
from app.scanner.utils import compute_download_stats, make_filename
from app.scanner.hb_scan import download_hb_with_captcha
from app.scanner.download import download_with_captcha
from app.scanner.gb_scan import extract_hcno
from app.dedup import get_existing_files, add_to_existing_files_cache
from config.settings import get_output_dir, DELAY

_log = logging.getLogger('std_scraper')

router = APIRouter(prefix="", tags=["Tasks"])


async def _do_retry_one(item, std_type, output_dir, existing):
    """重试下载单条标准，返回 (ok, msg)"""
    filename = make_filename(item.get('stdCode') or item.get('code', ''),
                             item.get('stdName') or item.get('name', ''))
    filepath = output_dir / filename

    if filename in existing:
        item['dlStatus'] = 'skipped_existing'
        return True, 'skipped_existing'

    if std_type == 'gb':
        hcno = item.get('hcno')
        if not hcno:
            await extract_hcno([item])
            hcno = item.get('hcno')
        if not hcno:
            item['dlStatus'] = 'failed_no_hcno'
            return False, 'failed_no_hcno'

        loop = asyncio.get_running_loop()
        pdf_data = await loop.run_in_executor(None, download_with_captcha, hcno)
        if pdf_data:
            atomic_write(str(filepath), pdf_data, dir_=str(output_dir))
            item['dlStatus'] = 'downloaded'
            item['fileSize'] = len(pdf_data)
            add_to_existing_files_cache(filename)
            return True, 'downloaded'
        else:
            item['dlStatus'] = 'failed'
            return False, 'failed'

    else:
        pk = item.get('pk')
        site_type = item.get('siteType', std_type)
        if not pk:
            item['dlStatus'] = 'failed_no_pk'
            return False, 'failed_no_pk'

        loop = asyncio.get_running_loop()
        pdf_data = await loop.run_in_executor(None, download_hb_with_captcha, pk, site_type)
        if pdf_data:
            atomic_write(str(filepath), pdf_data, dir_=str(output_dir))
            item['dlStatus'] = 'downloaded'
            add_to_existing_files_cache(filename)
            return True, 'downloaded'
        else:
            item['dlStatus'] = 'failed'
            return False, 'failed'


@router.get("/api/task/{task_id}")
async def get_task_status(task_id: str):
    """获取任务状态"""
    task = task_manager.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@router.get("/api/tasks")
async def get_all_tasks(status: Optional[str] = None):
    """获取所有任务（可按状态筛选）"""
    return task_manager.get_all(status_filter=status)


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

    if std_type in ('gb', 'hb', 'db'):
        config = {
            'max_results': max_results,
            'incr': incr,
            'keyword_group': keyword_group,
            'scan_only': scan_only,
            'industries': task.get('industries'),
            'provinces': task.get('provinces'),
        }
        result = _create_scan_task(
            scan_type=std_type,
            task_id_prefix=std_type,
            config=config,
        )
    elif std_type == 'all':
        # all 类型已废弃，改为创建三个独立任务
        scan_types = task.get('scan_types', ['gb', 'hb', 'db'])
        task_ids, scan_fn = create_combined_scan_tasks(
            scan_types=scan_types,
            max_results=max_results,
            incr=incr,
            keyword_group=keyword_group,
            scan_only=scan_only,
            hb_config={'industries': task.get('industries')},
            db_config={'provinces': task.get('provinces')},
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
            duration = max(0, end_time - task['start_time'] - task.get('paused_duration', 0))

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
    task = task_manager.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.get('status') == 'running':
        raise HTTPException(status_code=400, detail="任务正在运行，请等待完成后再重试单条")

    if task.get('std_type') == 'search':
        raise HTTPException(status_code=400, detail="搜索下载任务不支持单条重试")

    items = task.get('std_items', [])
    if item_index < 0 or item_index >= len(items):
        raise HTTPException(status_code=400, detail=f"索引 {item_index} 超出范围 (0-{len(items)-1})")

    std_type = task.get('std_type')
    keyword_group = task.get('keyword_group', '安全生产')
    set_active_group(keyword_group)

    output_dir = get_output_dir()
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    existing = get_existing_files()
    ok, status = await _do_retry_one(items[item_index], std_type, output_dir, existing)
    msg = f"重试 #{item_index}: {'成功' if ok else '失败'} ({status})"

    task_manager.update(task_id, std_items=items,
                      stats=compute_download_stats(items),
                      message=msg)
    return {"success": True, "status": status, "item_index": item_index, "message": msg}


@router.post("/api/task/{task_id}/retry-failed")
async def retry_all_failed(task_id: str):
    """批量重试所有下载失败的标准"""
    task = task_manager.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.get('status') == 'running':
        raise HTTPException(status_code=400, detail="任务正在运行，请等待完成后再重试")

    if task.get('std_type') == 'search':
        raise HTTPException(status_code=400, detail="搜索下载任务不支持批量重试")

    items = task.get('std_items', [])
    failed_indices = []
    for i, s in enumerate(items):
        ds = s.get('dlStatus', '')
        # failed_no_hcno / failed_no_pk 不可重试（hcno/pk 不会凭空出现）
        if ds and ds not in ('failed_no_hcno', 'failed_no_pk') and (ds == 'failed' or ds.startswith('failed_') or ds.startswith('error:')):
            failed_indices.append(i)

    if not failed_indices:
        return {"success": True, "retried": 0, "succeeded": 0, "failed": 0,
                "message": "没有需要重试的失败项"}

    std_type = task.get('std_type')
    keyword_group = task.get('keyword_group', '安全生产')
    set_active_group(keyword_group)

    output_dir = get_output_dir()
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    existing = get_existing_files()
    ok_count = 0
    fail_count = 0

    _log.info(f"批量重试 task={task_id} 失败项: {len(failed_indices)} 条")

    for i, idx in enumerate(failed_indices):
        ok, status = await _do_retry_one(items[idx], std_type, output_dir, existing)
        if ok:
            ok_count += 1
            # 下载成功后更新 existing 快照，防止同批次重复下载
            filename = make_filename(items[idx].get('stdCode') or items[idx].get('code', ''),
                                     items[idx].get('stdName') or items[idx].get('name', ''))
            existing.add(filename)
        else:
            fail_count += 1

        # 每处理一条就推送最新状态，让 UI 实时显示
        task_manager.update(task_id, std_items=items,
                          stats=compute_download_stats(items),
                          message=f"批量重试 {i+1}/{len(failed_indices)}: {'OK' if ok else 'FAIL'}")
        await asyncio.sleep(DELAY)

    task_manager.update(task_id, std_items=items,
                      stats=compute_download_stats(items),
                      message=f"批量重试完成: {ok_count} 成功, {fail_count} 失败")
    _log.info(f"批量重试完成: {ok_count}/{len(failed_indices)} 成功")
    return {"success": True, "retried": len(failed_indices),
            "succeeded": ok_count, "failed": fail_count}
