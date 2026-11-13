"""task_service — 任务生命周期编排

从 HTTP 路由层抽离的可复用逻辑：
  - 单条/批量重试下载（按任务类型分发到 GB/HB/DB/TT/MEM/搜索结果）
  - 搜索结果任务一键下载
  - 任务完成/失败的状态更新与通知（扫描、搜索共用）

路由层只负责参数校验与响应，task_manager 由调用方显式传入，
避免服务层反向依赖 app.routes。
"""
import asyncio
import logging
import time
from pathlib import Path

_log = logging.getLogger('std_scraper')

# 可重试状态白名单：
# - failed / failed_hcno / failed_preview / error:*  → 通用下载失败，重试可能成功
# - no_hcno / failed_no_hcno → 标准之前太新未发布，现在可能已分配 hcno
# 不可重试：copyright（站点限制）、failed_no_pk（标识不会变）、
# no_fulltext / preview_disabled（本身无全文）、已完成状态
RETRYABLE_STATUSES = ('failed', 'failed_hcno', 'failed_preview',
                      'failed_no_hcno', 'no_hcno')


class TaskStateError(ValueError):
    """任务状态不允许当前操作（路由层转换为 400）。"""


def complete_task(task_manager, task_id, message, stats=None, progress=100, **extra):
    """统一的任务完成状态更新。"""
    kw = {'progress': progress, 'status': 'completed',
          'message': message, 'end_time': time.time()}
    if stats is not None:
        kw['stats'] = stats
    kw.update(extra)
    task_manager.update(task_id, **kw)


def fail_task(task_manager, task_id, error):
    """统一的任务失败状态更新。"""
    task_manager.update(task_id, status='failed', message=str(error),
                        end_time=time.time())


async def notify_task_done(task_manager, task_id, message, stats=None):
    """任务完成后按配置渠道发送通知（线程执行，避免阻塞事件循环）。"""
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
        _log.debug(f"任务完成通知发送失败: {e}")


async def notify_task_failed(task_manager, task_id, error, title='任务失败'):
    """任务异常时的错误通知。"""
    try:
        from app.notifier import get_notification_service
        ns = get_notification_service()
        if not ns.notifiers:
            return
        await asyncio.to_thread(
            ns.send_message, title,
            f'任务 {task_id} 执行失败\n\n错误信息: {str(error)[:300]}', task_id)
    except Exception as e:
        _log.debug(f"任务失败通知发送失败: {e}")


async def retry_one_item(item, std_type, output_dir, existing):
    """重试下载单条标准，返回 (ok, status, reason)。

    reason 为简短中文失败原因，调用方可用于 UI 展示。
    支持 GB/HB/DB/TT/MEM 及搜索结果任务（search/search_batch）。
    """
    from app.scanner.utils import make_filename
    from app.scanner.download_helpers import fetch_and_save_pdf

    # 搜索结果任务按 item.tid 分发到 GB/HB/DB/TT 下载
    if std_type in ('search', 'search_batch'):
        from app.scanner.search_download import download_single_search_item
        return await download_single_search_item(item, output_dir)

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
            # 捕获非预期异常，防止单条异常中断整个批量重试
            item['dlStatus'] = 'failed'
            item['failReason'] = f'{type(e).__name__}: {e}'
            return False, 'failed', item['failReason']
        if pdf_data:
            item['dlStatus'] = 'downloaded'
            item['fileSize'] = len(pdf_data)
            item.pop('failReason', None)
            return True, 'downloaded', ''
        item['dlStatus'] = 'failed'
        item['failReason'] = reason_out.get('reason', '未知原因')
        return False, 'failed', item['failReason']

    # === HB/DB：pk 直接下载 ===
    if std_type in ('hb', 'db'):
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
            item['dlStatus'] = 'failed'
            item['failReason'] = f'{type(e).__name__}: {e}'
            return False, 'failed', item['failReason']
        if pdf_data:
            item['dlStatus'] = 'downloaded'
            item.pop('failReason', None)
            return True, 'downloaded', ''
        item['dlStatus'] = 'failed'
        item['failReason'] = reason_out.get('reason', '未知原因')
        return False, 'failed', item['failReason']

    # === TT：直连 API 下载 ===
    if std_type == 'tt':
        from app.scanner.tt_scan import _download_one_standard as _tt_download
        try:
            await _tt_download(item, existing)
        except Exception as e:
            item['dlStatus'] = 'failed'
            item['failReason'] = f'{type(e).__name__}: {e}'
            return False, 'failed', item['failReason']
        ok = item.get('dlStatus') == 'downloaded'
        return ok, item.get('dlStatus', 'failed'), item.get('failReason', '')

    # === MEM：直连 HTML 下载 ===
    if std_type == 'mem':
        from app.scanner.mem_scan import _download_one_standard as _mem_download
        try:
            await _mem_download(item, existing)
        except Exception as e:
            item['dlStatus'] = 'failed'
            item['failReason'] = f'{type(e).__name__}: {e}'
            return False, 'failed', item['failReason']
        ok = item.get('dlStatus') == 'downloaded'
        return ok, item.get('dlStatus', 'failed'), item.get('failReason', '')

    item['dlStatus'] = 'failed'
    item['failReason'] = f'不支持的任务类型: {std_type}'
    return False, 'failed', item['failReason']


async def retry_single_item(task_manager, task_id, item, item_index, std_type):
    """重试下载单条标准并回写任务状态，返回接口响应字典。"""
    from app.keywords import set_active_group
    from app.scanner.utils import compute_download_stats, dl_status_label
    from app.dedup import get_existing_files
    from config.settings import get_output_dir

    task = task_manager.get(task_id) or {}
    set_active_group(task.get('keyword_group', '安全生产'))

    output_dir = Path(get_output_dir())
    output_dir.mkdir(parents=True, exist_ok=True)
    items = task.get('std_items') or []

    code = item.get('stdCode') or item.get('code', '')
    name = item.get('stdName') or item.get('name', '')
    item_label = f"{code} {name}".strip()

    # 立即标记下载中并推送，前端可即时看到进度而不是无反馈等待
    item['dlStatus'] = 'downloading'
    item.pop('failReason', None)
    task_manager.update(task_id, std_items=items,
                        message=f'正在下载: {item_label}',
                        currentItem=item_label, persist_std_items=False)

    existing = await asyncio.get_running_loop().run_in_executor(None, get_existing_files)
    try:
        ok, status, reason = await retry_one_item(item, std_type, output_dir, existing)
    except Exception as e:
        # 单条异常不应让接口 500，统一转为失败状态供用户重试
        item['dlStatus'] = 'failed'
        item['failReason'] = f'{type(e).__name__}: {e}'
        ok, status, reason = False, 'failed', item['failReason']
    if item.get('dlStatus') == 'downloading':
        # 兜底：下载流程未写入终态时按失败处理，避免一直停留在"下载中"
        item['dlStatus'] = 'failed'
        item['failReason'] = item.get('failReason') or '下载流程未返回结果'
        ok, status, reason = False, 'failed', item['failReason']

    # 友好提示：成功显示状态，失败显示具体原因
    label = dl_status_label(status)
    msg = f"{item_label}：{label}" + ('' if ok else (f"（{reason}）" if reason else ""))

    task_manager.update(task_id, std_items=items,
                        stats=compute_download_stats(items),
                        message=msg, currentItem='')
    return {"success": True, "status": status, "item_index": item_index,
            "message": msg, "ok": ok, "reason": reason}


def retryable_indices(items):
    """筛出可重试的条目下标（见 RETRYABLE_STATUSES）。"""
    indices = []
    for i, item in enumerate(items):
        ds = item.get('dlStatus', '')
        if ds in RETRYABLE_STATUSES or ds.startswith('error:'):
            indices.append(i)
    return indices


async def retry_failed_items(task_manager, task_id, items, std_type):
    """批量重试失败项，返回接口响应字典。"""
    from app.keywords import set_active_group
    from app.scanner.utils import compute_download_stats, make_filename
    from app.dedup import get_existing_files
    from config.settings import get_output_dir, get_delay

    failed_indices = retryable_indices(items)
    if not failed_indices:
        return {"success": True, "retried": 0, "succeeded": 0, "failed": 0,
                "message": "没有需要重试的失败项"}

    task = task_manager.get(task_id) or {}
    set_active_group(task.get('keyword_group', '安全生产'))
    output_dir = Path(get_output_dir())
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = get_existing_files()

    ok_count = fail_count = 0
    fail_reasons = {}
    _log.info(f"批量重试 task={task_id} 失败项: {len(failed_indices)} 条")

    for i, idx in enumerate(failed_indices):
        target = items[idx]
        label = f"{target.get('stdCode') or target.get('code', '')} " \
                f"{target.get('stdName') or target.get('name', '')}".strip()
        task_manager.update(task_id,
                            message=f"批量重试 {i+1}/{len(failed_indices)}: {label}",
                            currentItem=label, persist_std_items=False)
        ok, status, reason = await retry_one_item(target, std_type, output_dir, existing)
        if ok:
            ok_count += 1
            # 下载成功后更新 existing 快照，防止同批次重复下载
            filename = make_filename(target.get('stdCode') or target.get('code', ''),
                                     target.get('stdName') or target.get('name', ''),
                                     suffix=target.get('file_ext') or '.pdf')
            existing.add(filename.lower())
        else:
            fail_count += 1
            if reason:
                fail_reasons[reason] = fail_reasons.get(reason, 0) + 1

        # 每处理一条就推送最新状态，让 UI 实时显示
        progress_msg = f"批量重试 {i+1}/{len(failed_indices)}: {label} " \
                       f"{'成功' if ok else '失败' + ('（' + reason + '）' if reason else '')}"
        task_manager.update(task_id, std_items=items,
                            stats=compute_download_stats(items),
                            message=progress_msg, persist_std_items=False)
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
                        message=summary_msg, currentItem='')
    _log.info(f"批量重试完成: {ok_count}/{len(failed_indices)} 成功, 失败原因: {fail_reasons}")
    return {"success": True, "retried": len(failed_indices),
            "succeeded": ok_count, "failed": fail_count,
            "fail_reasons": fail_reasons}


async def start_download_all(task_manager, task, skip_obsolete=True):
    """一键下载前的准备与状态切换，返回接口响应字段（含 launch 标记）。"""
    from app.scanner.search_download import (
        FINAL_STATUSES, mark_obsolete_skipped, mark_existing_skipped,
    )
    from app.scanner.utils import compute_download_stats, format_download_summary

    task_id = task['task_id']
    items = task.get('std_items') or []
    status = task.get('status')

    skipped_obsolete = mark_obsolete_skipped(items) if skip_obsolete else 0
    loop = asyncio.get_running_loop()
    skipped_existing = await loop.run_in_executor(None, mark_existing_skipped, items)
    count = sum(1 for it in items if it.get('dlStatus') not in FINAL_STATUSES)
    skip_notes = ''
    if skipped_obsolete:
        skip_notes += f"，跳过废止 {skipped_obsolete}"
    if skipped_existing:
        skip_notes += f"，本地已有 {skipped_existing}"

    if count == 0:
        stats = compute_download_stats(items)
        msg = format_download_summary(stats)
        task_manager.update(task_id, std_items=items, stats=stats, message=msg)
        return {"success": True, "task_id": task_id, "count": 0,
                "skipped_obsolete": skipped_obsolete,
                "skipped_existing": skipped_existing, "message": msg}

    # 暂停中的任务：不另起协程，直接唤醒原任务继续执行，避免两个下载循环并发。
    # 批量搜索任务额外打上 _download_only 标记，让搜索阶段立即收尾进入下载。
    if status == 'paused':
        if not task_manager.resume(task_id):
            raise TaskStateError("任务状态异常，请刷新后重试")
        task_manager.update(task_id, _download_only=True,
                            message=f'结束搜索，开始下载 {count} 个标准{skip_notes}',
                            std_items=items, stats=compute_download_stats(items),
                            end_time=None)
        _log.info(f"任务 {task_id}: 从暂停恢复并转入下载 {count} 个标准")
        return {"success": True, "task_id": task_id, "count": count,
                "skipped_obsolete": skipped_obsolete,
                "skipped_existing": skipped_existing, "resumed": True}

    task_manager.update(task_id, status='running', progress=0,
                        message=f'开始下载 {count} 个标准{skip_notes}',
                        std_items=items, stats=compute_download_stats(items),
                        end_time=None, paused_duration=0)
    return {"success": True, "task_id": task_id, "count": count,
            "skipped_obsolete": skipped_obsolete,
            "skipped_existing": skipped_existing, "launch": True, "items": items}


async def run_download_all(task_manager, task_id, items, count=None):
    """一键下载后台协程：下载全部条目并统一完成/失败状态与通知。"""
    from app.search_batch_service import download_selected_items
    from app.scanner.utils import compute_download_stats, format_download_summary

    try:
        _log.info(f"任务 {task_id}: 一键下载 {count or len(items)} 个标准")
        await download_selected_items(task_manager, task_id, items, progress_base=0)
        stats = compute_download_stats(items)
        msg = format_download_summary(stats)
        complete_task(task_manager, task_id, msg, stats=stats)
        _log.info(f"任务 {task_id}: 一键下载完成")
        await notify_task_done(task_manager, task_id, msg, stats)
    except Exception as e:
        _log.error(f"任务 {task_id} 一键下载异常: {e}")
        fail_task(task_manager, task_id, e)
        await notify_task_failed(task_manager, task_id, e)


async def resume_scan_download(task_manager, task_id):
    """扫描任务中断在下载阶段时，只续跑下载（不重新扫描）。

    仅 GB/HB/DB 支持；TT/MEM 的扫描与下载在同一管线内完成，
    调用方应改为重新执行完整扫描。
    """
    from app.scanner import (
        download_phase, download_hb_standards, download_db_standards,
        compute_download_stats, format_download_summary,
    )

    task = task_manager.get(task_id) or {}
    scan_type = task.get('std_type')
    standards = task.get('std_items') or []
    if scan_type not in ('gb', 'hb', 'db') or not standards:
        return False

    type_label = {'gb': '国家标准', 'hb': '行业标准', 'db': '地方标准'}.get(scan_type, scan_type)
    progress_base, progress_span = 50, 50

    async def _check_pause():
        while True:
            current = task_manager.get(task_id)
            if not current:
                return False
            status = current.get('status')
            if status == 'running':
                return True
            if status != 'paused':
                return False
            await asyncio.sleep(1)

    async def _on_progress(pct, msg):
        task_manager.update(
            task_id, dl_progress=pct,
            progress=progress_base + max(1, int(pct * progress_span / 100)),
            message=msg, persist_std_items=False)

    async def _on_item_done(item_name=''):
        kwargs = {'stats': compute_download_stats(standards), 'persist_std_items': False}
        if item_name:
            kwargs['currentItem'] = item_name
        task_manager.update(task_id, **kwargs)

    try:
        _log.info(f"任务 {task_id}: 从下载阶段续跑（{len(standards)} 条）")
        if scan_type == 'gb':
            await download_phase(standards,
                                 allow_preview_override=task.get('allow_preview'),
                                 on_progress=_on_progress, on_item_done=_on_item_done,
                                 check_pause=_check_pause)
        elif scan_type == 'hb':
            await download_hb_standards(standards, on_progress=_on_progress,
                                        on_item_done=_on_item_done, check_pause=_check_pause)
        else:
            await download_db_standards(standards, on_progress=_on_progress,
                                        on_item_done=_on_item_done, check_pause=_check_pause)

        if not await _check_pause():
            return False
        stats = compute_download_stats(standards)
        msg = format_download_summary(stats, prefix=f"{type_label}下载")
        complete_task(task_manager, task_id, msg, stats=stats, std_items=standards)
        _log.info(f"任务 {task_id}: 下载阶段续跑完成")
        await notify_task_done(task_manager, task_id, msg, stats)
        return True
    except Exception as e:
        _log.error(f"任务 {task_id} 续跑下载异常: {e}")
        fail_task(task_manager, task_id, e)
        await notify_task_failed(task_manager, task_id, e)
        return False


def create_download_restart_task(task_manager, task):
    """按已保存候选创建新的下载任务（重置下载状态，供"重新开始下载"）。

    Returns:
        (new_task_id, items)；无候选时返回 (None, [])。
    """
    from app.scanner.utils import compute_download_stats

    items = []
    for item in task.get('std_items') or []:
        copy = dict(item)
        copy.pop('dlStatus', None)
        copy.pop('failReason', None)
        items.append(copy)
    if not items:
        return None, []

    task_id = f"search_{int(time.time() * 1000)}"
    stats = compute_download_stats(items)
    task_manager.create(task_id, {
        "task_id": task_id,
        "status": "running",
        "progress": 0,
        "message": f"重新下载 {len(items)} 个标准",
        "stats": stats,
        "start_time": time.time(),
        "std_type": "search",
        "std_items": [],
    })
    return task_id, items
