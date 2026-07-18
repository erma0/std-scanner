"""API routes — Scheduled Jobs + 定时扫描执行逻辑

v3.5.0:
  - all 类型改为三个独立并发任务（不再用 run_combined_pipeline）
  - 扫描完成后发送汇总通知（区分全部成功/部分失败/全部失败）
"""
import asyncio
import time
import threading
import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

from app.scanner_engine import run_scan_pipeline
from config.manager import load_config
# 注意：不直接 from .state import _main_loop / ns —— 那会在 import 时按值快照为 None，
# 而 state._main_loop / state.ns 在 lifespan 中才被赋值/热重载时会被重新赋值。
# 通过模块属性按需读取，保证始终拿到最新实例。
from . import state as _state
from .state import task_manager as _task_manager, scheduler_mgr as _scheduler_mgr

_log = logging.getLogger('std_scraper')

router = APIRouter(prefix="", tags=["ScheduledJobs"])


# ==================== 定时扫描执行逻辑 ====================

async def _do_scheduled_scan_impl(scan_type, max_results, keyword_group, job_cfg, task_id=None):
    """定时扫描核心实现（所有类型默认增量扫描）

    v3.6.4: all 类型改为三个独立并发任务，支持独立暂停/恢复。
    支持暂停检查和 sub_stats 统计。
    """
    if scan_type == 'all':
        scan_types = job_cfg.get('scan_types', ['gb', 'hb', 'db'])
        hb_cfg = {'industries': job_cfg.get('industries')}
        db_cfg = {'provinces': job_cfg.get('provinces')}

        from ._utils import create_combined_scan_tasks
        task_ids, scan_fn = create_combined_scan_tasks(
            scan_types=scan_types, max_results=max_results,
            incr=True, keyword_group=keyword_group, scan_only=False,
            hb_config=hb_cfg, db_config=db_cfg,
        )
        await scan_fn()

        # 汇总通知
        ok_types = []
        fail_types = []
        for tid in task_ids:
            tk = _task_manager.get(tid)
            if not tk:
                fail_types.append(f"{tid}:任务丢失")
            elif tk.get('status') == 'completed':
                scanned = (tk.get('stats') or {}).get('scanned', 0)
                label = {'gb': '国家标准', 'hb': '行业标准', 'db': '地方标准'}.get(tk.get('std_type', ''), tk.get('std_type'))
                ok_types.append(f"{label}:{scanned}条")
            else:
                label = {'gb': '国家标准', 'hb': '行业标准', 'db': '地方标准'}.get(tk.get('std_type', ''), tk.get('std_type'))
                fail_types.append(f"{label}:{tk.get('message', '')[:30]}")

        if fail_types:
            title = "定时扫描完成（部分失败）" if ok_types else "⚠️ 定时扫描全部失败"
            content = ("成功: " + ", ".join(ok_types) + "\n失败: " + ", ".join(fail_types)) if ok_types else "失败: " + ", ".join(fail_types)
        else:
            title = "定时扫描完成"
            content = ", ".join(ok_types)

        ns = _state.ns
        if ns:
            try:
                ns.send_message(title, content)
            except Exception as ne:
                _log.error(f"发送定时扫描汇总通知失败: {ne}")

        _log.info(f"定时联合扫描完成: {title}")
    else:
        config = {
            'max_results': max_results, 'incr': True,
            'keyword_group': keyword_group,
            'industries': job_cfg.get('industries'),
            'provinces': job_cfg.get('provinces'),
        }
        standards = await run_scan_pipeline(scan_type, config,
                                            task_id=task_id, task_manager=_task_manager)
        _log.info(f"定时{scan_type.upper()}扫描完成: {len(standards)} 条")


def run_scheduled_scan(job_id: str, job_config: dict):
    """定时任务入口（由 APScheduler 在后台线程调用）。
    通过 asyncio.run_coroutine_threadsafe 将扫描任务提交到主事件循环。"""
    try:
        scan_type = job_config.get('type', 'gb')
        job_cfg = job_config.get('config', {})
        is_all = (scan_type == 'all')
        task_id = None

        if not is_all:
            task_id = f"{job_id}_{int(time.time())}"
            _task_manager.create(task_id, {
                "task_id": task_id,
                "status": "running",
                "progress": 0,
                "message": f"定时任务启动: {job_config.get('name', job_id)}",
                "stats": {"scanned": 0, "downloaded": 0, "success": 0, "failed": 0, "skipped": 0},
                "start_time": time.time(),
                "scheduled_job_id": job_id,
                "std_type": scan_type,
                "std_items": [],
                "keyword_group": job_cfg.get('keyword_group', '安全生产'),
                "max_results": job_cfg.get('max_results', 500),
                "incr": True,
                "scan_only": False,
                "paused_duration": 0,
            })
        _log.info(f"定时任务开始: {job_id}")

        async def _do_and_finish():
            try:
                await _do_scheduled_scan_impl(scan_type, job_cfg.get('max_results', 500),
                                            job_cfg.get('keyword_group', '安全生产'), job_cfg,
                                            task_id=task_id)
                if task_id:
                    _task_manager.update(task_id, status="completed", progress=100,
                                       message="定时任务完成", end_time=time.time())
                _log.info(f"定时任务完成: {job_id}")
            except Exception as e:
                _log.error(f"定时任务异常: {e}")
                if task_id:
                    _task_manager.update(task_id, status="failed", message=str(e), end_time=time.time())
                if _state.ns:
                    try:
                        _state.ns.send_message(
                            "⚠️ 定时任务执行错误",
                            f"任务 {job_id} 执行失败\n\n错误信息: {str(e)[:200]}\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                    except Exception as ne:
                        _log.error(f"发送错误通知失败: {ne}")

        main_loop = _state._main_loop
        if main_loop and main_loop.is_running():
            asyncio.run_coroutine_threadsafe(_do_and_finish(), main_loop)
        else:
            # 主事件循环未就绪：不再回退到独立 loop（run_scan_pipeline 内部依赖主循环状态，
            # 行为不一致且容易产生竞态）。直接标记任务失败并通知用户。
            _log.error(f"定时任务 {job_id} 启动失败：主事件循环未就绪")
            if task_id:
                _task_manager.update(task_id, status="failed",
                                     message="服务未就绪（主事件循环未运行），请稍后重试",
                                     end_time=time.time())
            if _state.ns:
                try:
                    _state.ns.send_message(
                        "⚠️ 定时任务无法执行",
                        f"任务 {job_id} 启动时主事件循环未就绪。\n"
                        f"可能原因：服务正在启动/关闭中。\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                except Exception as ne:
                    _log.error(f"发送错误通知失败: {ne}")
    except Exception as e:
        _log.error(f"定时任务启动失败: {job_id}, {e}")


def load_scheduled_jobs():
    return _scheduler_mgr.load_jobs()


def save_scheduled_jobs():
    _scheduler_mgr.save_jobs()


def init_config_logger():
    """根据配置文件设置日志级别和文件输出"""
    try:
        config = load_config()
        log_level = config.get('logging', {}).get('level', 'INFO')
        save_to_file = config.get('logging', {}).get('save_to_file', True)
        level = getattr(logging, log_level, logging.INFO)
        _log.setLevel(level)
        for h in _log.handlers[:]:
            if isinstance(h, logging.FileHandler):
                _log.removeHandler(h)
        if save_to_file:
            from config.paths import LOG_DIR
            log_dir = LOG_DIR
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / f'std_scraper_{datetime.now().strftime("%Y%m%d")}.log'
            file_handler = logging.FileHandler(log_file, encoding='utf-8')
            file_handler.setLevel(level)
            file_handler.setFormatter(logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            ))
            _log.addHandler(file_handler)
    except Exception as e:
        _log.warning(f"加载配置设置日志失败，使用默认值: {e}")


# ==================== API 路由 ====================

@router.get("/api/scheduled_jobs")
async def get_scheduled_jobs():
    """获取所有定时任务"""
    if not _scheduler_mgr.available:
        return {"success": False, "error": "APScheduler 未安装"}
    jobs = _scheduler_mgr.get_all_jobs()
    job_statuses = _scheduler_mgr.get_next_run_times()
    result = []
    for job_id, job_config in jobs.items():
        result.append({
            "id": job_id,
            **job_config,
            "next_run_time": job_statuses.get(job_id)
        })
    return {"success": True, "jobs": result}


@router.post("/api/scheduled_jobs")
async def create_scheduled_job(request: Request):
    """创建定时任务"""
    if not _scheduler_mgr.available:
        raise HTTPException(status_code=500, detail="APScheduler 未安装")
    body = await request.json()
    job_id = body.get("id", f"job_{int(time.time())}")
    job_name = body.get("name", "定时扫描任务")
    job_type = body.get("type", "gb")
    cron = body.get("cron", "0 8 * * *")
    enabled = body.get("enabled", True)
    job_config_data = body.get("config", {})
    job_config = {
        "name": job_name,
        "type": job_type,
        "cron": cron,
        "enabled": enabled,
        "config": job_config_data,
        "created_at": datetime.now().isoformat()
    }
    success = _scheduler_mgr.add_job(job_id, job_config, run_fn=run_scheduled_scan)
    if not success:
        raise HTTPException(status_code=400, detail="添加定时任务失败")
    return {"success": True, "job_id": job_id, "job": job_config}


@router.put("/api/scheduled_jobs/{job_id}")
async def update_scheduled_job(job_id: str, request: Request):
    """更新定时任务"""
    if not _scheduler_mgr.available:
        raise HTTPException(status_code=500, detail="APScheduler 未安装")
    job = _scheduler_mgr.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="定时任务不存在")
    body = await request.json()
    job_config = job.copy()
    if "name" in body:
        job_config["name"] = body["name"]
    if "type" in body:
        job_config["type"] = body["type"]
    if "cron" in body:
        job_config["cron"] = body["cron"]
    if "enabled" in body:
        job_config["enabled"] = body["enabled"]
    if "config" in body:
        job_config["config"] = body["config"]
    job_config["updated_at"] = datetime.now().isoformat()
    success = _scheduler_mgr.update_job(job_id, job_config, run_fn=run_scheduled_scan)
    if not success:
        raise HTTPException(status_code=400, detail="更新定时任务失败")
    return {"success": True, "job": job_config}


@router.delete("/api/scheduled_jobs/{job_id}")
async def delete_scheduled_job(job_id: str):
    """删除定时任务"""
    if not _scheduler_mgr.available:
        raise HTTPException(status_code=500, detail="APScheduler 未安装")
    job = _scheduler_mgr.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="定时任务不存在")
    success = _scheduler_mgr.remove_job(job_id)
    if not success:
        raise HTTPException(status_code=400, detail="删除定时任务失败")
    return {"success": True}


@router.post("/api/scheduled_jobs/{job_id}/run")
async def run_scheduled_job_now(job_id: str):
    """立即运行定时任务"""
    if not _scheduler_mgr.available:
        raise HTTPException(status_code=500, detail="APScheduler 未安装")
    job = _scheduler_mgr.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="定时任务不存在")
    threading.Thread(target=lambda: run_scheduled_scan(job_id, job), name=f"scheduled-{job_id}", daemon=True).start()
    return {"success": True, "message": "任务已触发"}
