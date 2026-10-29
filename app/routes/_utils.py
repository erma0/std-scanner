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


def _mem_task_fields(scan_type, mem_config):
    """生成 mem 任务对象所需的 site/sites 字段（其他类型返回空 dict）。"""
    if scan_type != 'mem':
        return {}
    mem_config = mem_config or {}
    sites_cfg = mem_config.get('sites')
    if isinstance(sites_cfg, list) and len(sites_cfg) > 0:
        # 多站点：保存 mem_sites，并以首个 site 作为兼容 mem_site
        return {'mem_sites': sites_cfg, 'mem_site': sites_cfg[0].get('site', 'mem')}
    return {'mem_site': mem_config.get('site', 'mem'),
            'mem_source': mem_config.get('source', 'bz')}


def create_combined_scan_tasks(scan_types, max_results, incr, keyword_group,
                                scan_only, gb_config=None, hb_config=None,
                                db_config=None, tt_config=None, mem_config=None,
                                allow_preview=None, std_state='现行'):
    """创建联合扫描任务（GB+HB+DB+TT+MEM）— 每个类型独立任务，asyncio.gather 并发执行。

    供 scan_all / retry_all / 定时扫描复用。

    Returns:
        (task_ids: list[str], async_fn): task_ids 为各任务 ID 列表，
        caller 将 async_fn 传给 launch_task() 执行。
    """
    from .state import task_manager
    from app.scanner_engine import run_scan_pipeline
    from app.scanner.utils import compute_download_stats, format_download_summary
    from app.notifier import get_notification_service
    from app.helpers import format_duration

    ts = int(time.time())
    type_configs = {
        'gb': {'config': gb_config or {}, 'allow_preview': allow_preview},
        'hb': {'config': hb_config or {}},
        'db': {'config': db_config or {}},
        'tt': {'config': tt_config or {}},
        'mem': {'config': mem_config or {}},
    }

    _type_label = {'gb': '国家标准', 'hb': '行业标准', 'db': '地方标准', 'tt': '团体标准', 'mem': '应急管理部标准'}

    task_ids = []
    for st in scan_types:
        tid = f"{st}_{ts}"

        # mem 类型按 site 区分标签（支持多站点）
        _MEM_LABELS = {'mem': '应急管理部标准', 'samr': '市场监管总局法规'}
        if st == 'mem':
            sites_cfg = (mem_config or {}).get('sites')
            if isinstance(sites_cfg, list) and len(sites_cfg) > 1:
                label = '+'.join(_MEM_LABELS.get(t.get('site'), '政策法规') for t in sites_cfg)
            else:
                mem_site = (mem_config or {}).get('site', 'mem')
                label = _MEM_LABELS.get(mem_site, '应急管理部标准')
        else:
            label = _type_label.get(st, st)

        task_manager.create(tid, {
            "task_id": tid,
            "status": "running",
            "progress": 0,
            "message": f"开始扫描{label}",
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
            **_mem_task_fields(st, mem_config),
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
                'cnl1_codes': tc.get('config', {}).get('cnl1_codes'),
            }
            # MEM 透传：支持 sites 列表（多站点）或 site/source（单站点）
            if st == 'mem':
                mem_cfg = tc.get('config', {})
                sites_cfg = mem_cfg.get('sites')
                if isinstance(sites_cfg, list) and len(sites_cfg) > 0:
                    # 多站点模式：透传 sites，过滤无效项
                    valid = []
                    for t in sites_cfg:
                        s = t.get('site', 'mem')
                        src = t.get('source', 'bz')
                        if s == 'samr':
                            src = src if src in ('flfg', 'aqjsgf', 'all') else 'aqjsgf'
                        else:
                            s = 'mem'
                            src = src if src in ('bz', 'gz', 'all') else 'bz'
                        valid.append({'site': s, 'source': src})
                    if len(valid) == 1:
                        config['site'] = valid[0]['site']
                        config['source'] = valid[0]['source']
                    else:
                        config['sites'] = valid
                else:
                    mem_site = mem_cfg.get('site', 'mem')
                    config['site'] = mem_site if mem_site in ('mem', 'samr') else 'mem'
                    mem_src = mem_cfg.get('source', 'bz')
                    if config['site'] == 'samr':
                        config['source'] = mem_src if mem_src in ('flfg', 'aqjsgf', 'all') else 'aqjsgf'
                    else:
                        config['source'] = mem_src if mem_src in ('bz', 'gz', 'all') else 'bz'
            if st == 'gb' and allow_preview is not None:
                config['allow_preview'] = allow_preview

            # 动态计算标签（mem 按 site 区分）
            if st == 'mem':
                _MEM_LABELS = {'mem': '应急管理部标准', 'samr': '市场监管总局法规'}
                sites_cfg = config.get('sites')
                if isinstance(sites_cfg, list) and len(sites_cfg) > 1:
                    _label = '+'.join(_MEM_LABELS.get(t.get('site'), '政策法规') for t in sites_cfg)
                else:
                    _label = _MEM_LABELS.get(config.get('site'), '应急管理部标准')
            else:
                _label = _type_label.get(st, st)

            try:
                standards = await run_scan_pipeline(
                    scan_type=st, config=config,
                    task_id=tid, task_manager=task_manager,
                )

                # 扫描结果为空：区分"无新增（增量命中）"和"无符合条件标准"
                if not standards:
                    state_label = std_state or '全部'
                    empty_msg = '无新增标准' if incr else '无符合条件标准'
                    task_manager.update(tid,
                        stats={'scanned': 0, 'downloaded': 0, 'success': 0, 'failed': 0, 'skipped': 0},
                        std_items=[],
                        progress=100, status="completed",
                        end_time=time.time(),
                        message=f"{empty_msg}（{state_label}），跳过下载")
                    _log.info(f"联合子任务 {tid}: {empty_msg}（{state_label}），跳过下载")
                    return {'ok': True, 'st': st, 'cnt': 0, 'empty': True}

                # 统计下载结果
                stats = compute_download_stats(standards)

                task_manager.update(tid,
                    stats=stats, std_items=standards,
                    progress=100, status="completed",
                    end_time=time.time(),
                    message=format_download_summary(stats, prefix=f"{_label}下载"))

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

        # 汇总日志：区分 完成任务 / 空任务（无符合条件标准） / 失败任务
        ok_types = []
        empty_types = []
        fail_types = []
        for r in results:
            if not r.get('ok'):
                fail_types.append(f"{r['st']}:{r.get('error', '')}")
            elif r.get('empty'):
                empty_types.append(r['st'])
            else:
                ok_types.append(f"{r['st']}:{r['cnt']}条")

        summary_parts = []
        if ok_types:
            summary_parts.append(f"成功={ok_types}")
        if empty_types:
            summary_parts.append(f"无符合条件标准={empty_types}")
        if fail_types:
            summary_parts.append(f"失败={fail_types}")
        _log.info(f"联合扫描完成: {', '.join(summary_parts) if summary_parts else '全部空'}")
        task_manager.save_all()

    return task_ids, _scan_all
