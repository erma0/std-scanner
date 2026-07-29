"""API routes — Tasks"""
from fastapi import APIRouter, HTTPException, Request
from typing import Optional
import time
import asyncio
import os
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


async def _do_retry_one(item, std_type, output_dir, existing):
    """重试下载单条标准，返回 (ok, status, reason)

    reason 为简短中文失败原因，调用方可用于 UI 展示
    支持 GB/HB/DB/TT/MEM 五种类型
    """
    from app.scanner.utils import make_filename
    from app.scanner.download_helpers import fetch_and_save_pdf

    # TT/MEM 使用 code/name 字段，GB/HB/DB 使用 stdCode/stdName
    code = item.get('stdCode') or item.get('code', '')
    name = item.get('stdName') or item.get('name', '')

    # MEM 可能用 doc/docx 扩展名
    file_ext = item.get('file_ext') or '.pdf'
    filename = make_filename(code, name, suffix=file_ext)
    filepath = output_dir / filename

    if filename.lower() in existing:
        item['dlStatus'] = 'skipped_existing'
        item.pop('failReason', None)
        return True, 'skipped_existing', '文件已存在'

    # === GB：验证码下载 ===
    if std_type == 'gb':
        from app.scanner.gb_scan import extract_hcno
        from app.scanner.download import download_with_captcha
        hcno = item.get('hcno')
        if not hcno:
            try:
                await extract_hcno([item])
            except Exception as e:
                item['dlStatus'] = 'failed_hcno'
                item['failReason'] = f'hcno 提取失败: {type(e).__name__}'
                return False, 'failed_hcno', item['failReason']
            hcno = item.get('hcno')
        if not hcno:
            item['dlStatus'] = 'failed_no_hcno'
            item['failReason'] = 'hcno 未分配（标准太新）'
            return False, 'failed_no_hcno', item['failReason']

        loop = asyncio.get_running_loop()
        reason_out = {}
        try:
            pdf_data = await loop.run_in_executor(
                None, fetch_and_save_pdf,
                lambda: download_with_captcha(hcno, reason_out=reason_out),
                filepath, filename, output_dir)
        except Exception as e:
            # 捕获 download_with_captcha / fetch_and_save_pdf 抛出的非预期异常
            # 防止单条异常中断整个批量重试
            item['dlStatus'] = 'failed'
            item['failReason'] = f'{type(e).__name__}: {e}'
            return False, 'failed', item['failReason']
        if pdf_data:
            item['dlStatus'] = 'downloaded'
            item['fileSize'] = len(pdf_data)
            item.pop('failReason', None)
            return True, 'downloaded', ''
        else:
            item['dlStatus'] = 'failed'
            item['failReason'] = reason_out.get('reason', '未知原因')
            return False, 'failed', item['failReason']

    # === HB/DB：pk 直接下载 ===
    elif std_type in ('hb', 'db'):
        from app.scanner.hb_scan import download_hb_with_captcha, CopyrightError
        pk = item.get('pk')
        site_type = item.get('siteType', std_type)
        if not pk:
            item['dlStatus'] = 'failed_no_pk'
            item['failReason'] = '无 pk 标识'
            return False, 'failed_no_pk', item['failReason']

        loop = asyncio.get_running_loop()
        reason_out = {}
        try:
            pdf_data = await loop.run_in_executor(
                None, fetch_and_save_pdf,
                lambda: download_hb_with_captcha(pk, site_type, reason_out=reason_out),
                filepath, filename, output_dir)
        except CopyrightError as e:
            item['dlStatus'] = 'copyright'
            item['failReason'] = f'版权限制: {e}'
            return False, 'copyright', item['failReason']
        except Exception as e:
            # 捕获非预期异常，防止单条异常中断整个批量重试
            item['dlStatus'] = 'failed'
            item['failReason'] = f'{type(e).__name__}: {e}'
            return False, 'failed', item['failReason']
        if pdf_data:
            item['dlStatus'] = 'downloaded'
            item.pop('failReason', None)
            return True, 'downloaded', ''
        else:
            item['dlStatus'] = 'failed'
            item['failReason'] = reason_out.get('reason', '未知原因')
            return False, 'failed', item['failReason']

    # === TT：直连 API 下载 ===
    elif std_type == 'tt':
        from app.scanner.tt_scan import _download_one_standard as _tt_download
        # _download_one_standard 内部已设置 dlStatus/failReason
        try:
            await _tt_download(item, existing)
        except Exception as e:
            item['dlStatus'] = 'failed'
            item['failReason'] = f'{type(e).__name__}: {e}'
            return False, 'failed', item['failReason']
        ok = item.get('dlStatus') == 'downloaded'
        return ok, item.get('dlStatus', 'failed'), item.get('failReason', '')

    # === MEM：直连 HTML 下载 ===
    elif std_type == 'mem':
        from app.scanner.mem_scan import _download_one_standard as _mem_download
        try:
            await _mem_download(item, existing)
        except Exception as e:
            item['dlStatus'] = 'failed'
            item['failReason'] = f'{type(e).__name__}: {e}'
            return False, 'failed', item['failReason']
        ok = item.get('dlStatus') == 'downloaded'
        return ok, item.get('dlStatus', 'failed'), item.get('failReason', '')

    else:
        item['dlStatus'] = 'failed'
        item['failReason'] = f'不支持的任务类型: {std_type}'
        return False, 'failed', item['failReason']


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
    from app.keywords import set_active_group
    from app.scanner.utils import compute_download_stats
    from app.dedup import get_existing_files
    from config.settings import get_output_dir

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
    ok, status, reason = await _do_retry_one(items[item_index], std_type, output_dir, existing)
    # 友好提示：成功显示状态，失败显示具体原因
    if ok:
        msg = f"重试 #{item_index}: 成功 ({status})"
    else:
        msg = f"重试 #{item_index}: 失败 ({status}) - {reason}" if reason else f"重试 #{item_index}: 失败 ({status})"

    task_manager.update(task_id, std_items=items,
                      stats=compute_download_stats(items),
                      message=msg)
    return {"success": True, "status": status, "item_index": item_index,
            "message": msg, "ok": ok, "reason": reason}


@router.post("/api/task/{task_id}/retry-failed")
async def retry_all_failed(task_id: str):
    """批量重试所有下载失败的标准"""
    from app.keywords import set_active_group
    from app.scanner.utils import compute_download_stats, make_filename
    from app.dedup import get_existing_files
    from config.settings import get_output_dir, get_delay

    task = task_manager.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.get('status') == 'running':
        raise HTTPException(status_code=400, detail="任务正在运行，请等待完成后再重试")

    if task.get('std_type') == 'search':
        raise HTTPException(status_code=400, detail="搜索下载任务不支持批量重试")

    items = task.get('std_items', [])
    failed_indices = []
    # 可重试状态白名单（仅这些状态会进入批量重试）：
    # - failed / failed_hcno / failed_preview / error:*  → 通用下载失败，重试可能成功
    # - no_hcno → 标准之前太新未发布到 openstd，现在可能已分配 hcno
    # - failed_no_hcno → GB 重试时会重新尝试提取 hcno（标准可能已上线）
    # 不可重试状态（白名单外自动跳过）：
    # - copyright → 版权保护是网站限制，重试无意义
    # - failed_no_pk → HB/DB 的 pk 标识不会变，重试无意义
    # - no_fulltext / preview_disabled → 标准本身不提供全文
    # - downloaded / skipped_existing / previewed → 已成功，无需重试
    RETRYABLE_STATUSES = ('failed', 'failed_hcno', 'failed_preview', 'failed_no_hcno', 'no_hcno')
    for i, s in enumerate(items):
        ds = s.get('dlStatus', '')
        if ds in RETRYABLE_STATUSES or ds.startswith('error:'):
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
    # 失败原因汇总（按原因计数）
    fail_reasons = {}

    _log.info(f"批量重试 task={task_id} 失败项: {len(failed_indices)} 条")

    for i, idx in enumerate(failed_indices):
        ok, status, reason = await _do_retry_one(items[idx], std_type, output_dir, existing)
        if ok:
            ok_count += 1
            # 下载成功后更新 existing 快照，防止同批次重复下载
            filename = make_filename(items[idx].get('stdCode') or items[idx].get('code', ''),
                                     items[idx].get('stdName') or items[idx].get('name', ''),
                                     suffix=items[idx].get('file_ext') or '.pdf')
            existing.add(filename.lower())
        else:
            fail_count += 1
            if reason:
                fail_reasons[reason] = fail_reasons.get(reason, 0) + 1

        # 每处理一条就推送最新状态，让 UI 实时显示
        progress_msg = f"批量重试 {i+1}/{len(failed_indices)}: {'OK' if ok else 'FAIL' + (' - ' + reason if reason else '')}"
        task_manager.update(task_id, std_items=items,
                          stats=compute_download_stats(items),
                          message=progress_msg)
        await asyncio.sleep(get_delay())

    # 汇总消息：失败原因按计数降序展示前 3 个
    summary_parts = [f"{ok_count} 成功", f"{fail_count} 失败"]
    if fail_reasons:
        top_reasons = sorted(fail_reasons.items(), key=lambda x: -x[1])[:3]
        reasons_str = '；'.join(f"{r}({c}条)" for r, c in top_reasons)
        summary_parts.append(f"主要原因: {reasons_str}")
    summary_msg = "批量重试完成: " + '，'.join(summary_parts)

    task_manager.update(task_id, std_items=items,
                      stats=compute_download_stats(items),
                      message=summary_msg)
    _log.info(f"批量重试完成: {ok_count}/{len(failed_indices)} 成功, 失败原因: {fail_reasons}")
    return {"success": True, "retried": len(failed_indices),
            "succeeded": ok_count, "failed": fail_count,
            "fail_reasons": fail_reasons}
