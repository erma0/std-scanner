"""路由模块共享工具函数（避免循环导入）"""
import asyncio
import time
import logging
from datetime import datetime

_log = logging.getLogger('std_scraper')


def update_task_status(task_id, progress=None, message=None, status=None, stats=None, **kwargs):
    """更新任务状态（薄包装 task_manager.update）"""
    from .state import task_manager
    kw = {}
    if progress is not None: kw['progress'] = progress
    if message is not None: kw['message'] = message
    if status is not None: kw['status'] = status
    if stats is not None: kw['stats'] = stats
    kw.update(kwargs)
    task_manager.update(task_id, **kw)


def launch_task(coro, name="unnamed"):
    """创建后台任务并添加异常日志回调"""
    task = asyncio.create_task(coro)
    task.add_done_callback(lambda t: (
        _log.error(f"后台任务 [{name}] 未处理异常: {t.exception()}")
        if not t.cancelled() and t.exception() else None
    ))
    return task


def create_combined_scan_tasks(scan_types, max_results, incr, keyword_group,
                                scan_only, gb_config=None, hb_config=None,
                                db_config=None, allow_preview=None, std_state='现行'):
    """创建联合扫描任务（GB+HB+DB）— 每个类型独立任务，asyncio.gather 并发执行。

    供 scan_all / retry_all / 定时扫描复用。

    Returns:
        (task_ids: list[str], async_fn): task_ids 为三个任务的 ID 列表，
        caller 将 async_fn 传给 launch_task() 执行。
    """
    from .state import task_manager
    from app.scanner_engine import run_scan_pipeline
    from app.scanner.utils import compute_download_stats
    from app.notifier import get_notification_service
    from app.helpers import format_duration

    ts = int(time.time())
    type_configs = {
        'gb': {'config': gb_config or {}, 'allow_preview': allow_preview},
        'hb': {'config': hb_config or {}},
        'db': {'config': db_config or {}},
    }

    task_ids = []
    for st in scan_types:
        tid = f"{st}_{ts}"

        task_manager.create(tid, {
            "task_id": tid,
            "status": "running",
            "progress": 0,
            "message": f"开始扫描{'国家标准' if st == 'gb' else '行业标准' if st == 'hb' else '地方标准'}",
            "stats": {"scanned": 0, "downloaded": 0, "success": 0, "failed": 0, "skipped": 0},
            "start_time": time.time(),
            "std_type": st,
            "resume_from": 0,
            "std_items": [],
            "keyword_group": keyword_group,
            "max_results": max_results,
            "incr": incr,
            "scan_only": scan_only,
            "paused_duration": 0,
        })
        task_ids.append(tid)

    async def _scan_all():
        """并发执行所有子任务"""
        ns = get_notification_service()

        async def _run_one(st, tid):
            tc = type_configs.get(st, {})
            config = {
                'max_results': max_results, 'incr': incr,
                'keyword_group': keyword_group, 'scan_only': scan_only,
                'std_state': std_state,
                'industries': tc.get('config', {}).get('industries'),
                'provinces': tc.get('config', {}).get('provinces'),
            }
            if st == 'gb' and allow_preview is not None:
                config['allow_preview'] = allow_preview

            try:
                standards = await run_scan_pipeline(
                    scan_type=st, config=config,
                    task_id=tid, task_manager=task_manager,
                )

                # 统计下载结果
                stats = compute_download_stats(standards)

                task_manager.update(tid,
                    stats=stats, std_items=standards,
                    progress=100, status="completed",
                    end_time=time.time(),
                    message=f"{'国家标准' if st == 'gb' else '行业标准' if st == 'hb' else '地方标准'}处理完成({len(standards)}条)")

                # 发送通知
                task = task_manager.get(tid)
                report = stats.copy()
                report['time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if task and task.get('start_time') and task.get('end_time'):
                    duration = task['end_time'] - task['start_time']
                    report['duration'] = format_duration(duration)
                ns.send_report(report, task_id=tid)
                _log.info(f"联合子任务 {tid}: 完成")
                return {'ok': True, 'st': st, 'cnt': len(standards)}

            except Exception as e:
                _log.error(f"联合子任务 {tid} 异常: {e}")
                task_manager.update(tid, status="failed", message=str(e)[:200],
                                  end_time=time.time())
                return {'ok': False, 'st': st, 'error': str(e)[:200]}

        results = await asyncio.gather(
            *[_run_one(st, tid) for st, tid in zip(scan_types, task_ids)]
        )

        # 汇总日志
        ok_types = []
        fail_types = []
        for r in results:
            if r.get('ok'):
                ok_types.append(f"{r['st']}:{r['cnt']}条")
            else:
                fail_types.append(f"{r['st']}:{r.get('error', '')}")

        _log.info(f"联合扫描完成: 成功={ok_types}, 失败={fail_types}")
        task_manager.save_all()

    return task_ids, _scan_all
