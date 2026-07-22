"""API routes — Scan

统一扫描入口：所有 GB/HB/DB 单独扫描和联合扫描都通过 run_scan_pipeline 执行，
消除此前 _create_scan_task + batch_download 与 run_scan_pipeline + download_phase
的双路径问题。
"""
from fastapi import APIRouter, HTTPException, Query
from typing import Optional, List
from pydantic import BaseModel, Field
import time
import logging
from datetime import datetime

from .state import task_manager
from ._utils import launch_task as _launch_task, create_combined_scan_tasks
# app.scanner_engine 导入耗时长（~0.7s，含 GB/HB/DB/TT 扫描器），延迟到首次使用时
# compute_download_stats 也延迟导入（app.scanner.utils 触发 captcha/preview 等链，约 250ms）
from app.notifier import get_notification_service
from config.settings import _resolve_hb_industry
from app.helpers import format_duration

_log = logging.getLogger('std_scraper')

router = APIRouter(prefix="", tags=["Scan"])


# 普通模式 max_results 上限；__all__ 模式（不过滤关键词）放开到 _MAX_RESULTS_ALL
_MAX_RESULTS_NORMAL = 5000
_MAX_RESULTS_ALL = 100000


class ScanAllRequest(BaseModel):
    types: List[str] = Field(default=["gb", "hb", "db"], description="扫描类型列表")
    scan_only: bool = Field(default=False, description="仅扫描不下载")
    incr: bool = Field(default=False, description="增量扫描")
    max_results: int = Field(default=500, ge=1, le=_MAX_RESULTS_ALL, description="每种类型最大扫描条数（HB/DB为每个行业/省份各自上限；普通模式上限 5000，__all__ 模式放开到 100000）")
    keyword_group: str = Field(default="安全生产", description="关键词组名（特殊值 '__all__' 跳过关键词过滤）")
    std_state: str = Field(default="现行", description="标准状态筛选: ''(全部)/现行/即将实施/废止")
    allow_preview: Optional[bool] = Field(default=None, description="允许预览拼接下载，仅对国标GB生效（None=使用全局配置）")
    gb_config: dict = Field(default={}, description="国标额外配置")
    hb_config: dict = Field(default={}, description="行标额外配置")
    db_config: dict = Field(default={}, description="地标额外配置")
    tt_config: dict = Field(default={}, description="团体标准额外配置（如 cnl1_codes）")
    mem_config: dict = Field(default={}, description="应急管理部标准额外配置")


def _check_max_results(keyword_group: str, max_results: int):
    """普通关键词组模式下 max_results 上限 5000；__all__ 模式放开到 100000"""
    if keyword_group == '__all__':
        return
    if max_results > _MAX_RESULTS_NORMAL:
        raise HTTPException(
            status_code=400,
            detail=f"普通模式下 max_results 上限 {_MAX_RESULTS_NORMAL}，使用 '__all__' 关键词组可放开至 {_MAX_RESULTS_ALL}",
        )


def _create_scan_task(scan_type, task_id_prefix, config, resume_task_id=None):
    """创建扫描任务（统一入口）。

    所有类型（GB/HB/DB/TT/MEM）都通过 run_scan_pipeline 执行，
    确保扫描→提取→下载的编排逻辑一致。

    Args:
        scan_type: 'gb' | 'hb' | 'db' | 'tt' | 'mem'
        task_id_prefix: 任务 ID 前缀（gb/hb/db/tt/mem）
        config: 扫描配置字典，含 max_results/incr/keyword_group/industries/provinces/cnl1_codes 等
        resume_task_id: 可选，恢复已有任务
    """
    task_id = resume_task_id or f"{task_id_prefix}_{int(time.time())}"

    type_label = {'gb': '国家标准', 'hb': '行业标准', 'db': '地方标准', 'tt': '团体标准', 'mem': '应急管理部标准'}[scan_type]

    retry_config = {
        'keyword_group': config.get('keyword_group', '安全生产'),
        'max_results': config.get('max_results', 500),
        'incr': config.get('incr', False),
        'scan_only': config.get('scan_only', False),
        'std_state': config.get('std_state', '现行'),
        'industries': config.get('industries'),
        'provinces': config.get('provinces'),
        'cnl1_codes': config.get('cnl1_codes'),
    }

    if not task_manager.exists(task_id):
        task_manager.create(task_id, {
            "task_id": task_id,
            "status": "running",
            "progress": 0,
            "message": f"开始扫描{type_label}",
            "stats": {"scanned": 0, "downloaded": 0, "success": 0, "failed": 0, "skipped": 0},
            "start_time": time.time(),
            "std_type": task_id_prefix,
            "resume_from": 0,
            "std_items": [],
            "paused_duration": 0,
            **retry_config,
        })

    async def scan_task():
        try:
            _log.info(f"任务 {task_id}: 开始扫描{type_label}")
            from app.scanner_engine import run_scan_pipeline

            standards = await run_scan_pipeline(
                scan_type=scan_type,
                config=config,
                task_id=task_id,
                task_manager=task_manager,
            )

            # 扫描结果为空：标识"无符合条件标准"，不计入下载统计
            if not standards:
                std_state = config.get('std_state', '现行')
                state_label = std_state or '全部'
                task_manager.update(task_id,
                    stats={'scanned': 0, 'downloaded': 0, 'success': 0, 'failed': 0, 'skipped': 0},
                    std_items=[],
                    progress=100, status="completed",
                    end_time=time.time(),
                    message=f"无符合条件标准（{state_label}），跳过下载")
                _log.info(f"任务 {task_id}: 无符合条件标准（{state_label}），跳过下载")
                task_manager.save_all()
                return

            from app.scanner.utils import compute_download_stats
            task_manager.update(task_id,
                stats=compute_download_stats(standards),
                std_items=standards,
                progress=100, status="completed",
                end_time=time.time(),
                message=f"{type_label}下载完成({len(standards)}条)")

            task = task_manager.get(task_id)
            report = task['stats'].copy()
            report['time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if task.get('start_time') and task.get('end_time'):
                duration = task['end_time'] - task['start_time']
                report['duration'] = format_duration(duration)

            ns = get_notification_service()
            ns.send_report(report, task_id=task_id)

            _log.info(f"任务 {task_id}: 完成")
            task_manager.save_all()

        except Exception as e:
            _log.error(f"任务 {task_id} 异常: {e}")
            task_manager.update(task_id, status="failed", message=str(e), end_time=time.time())
            send_error_alert(task_id, str(e))

    _launch_task(scan_task(), f"scan-{task_id}")
    return {"success": True, "task_id": task_id}


@router.post("/api/scan/gb")
async def scan_gb_standards_api(max_results: int = 500, scan_only: bool = False, incr: bool = False, resume_task_id: Optional[str] = None, keyword_group: str = '安全生产', allow_preview: Optional[bool] = None, std_state: str = '现行'):
    _check_max_results(keyword_group, max_results)
    config = {
        'max_results': max_results,
        'incr': incr,
        'keyword_group': keyword_group,
        'scan_only': scan_only,
        'allow_preview': allow_preview,
        'std_state': std_state,
    }
    return _create_scan_task('gb', 'gb', config, resume_task_id=resume_task_id)


@router.post("/api/scan/hb")
async def scan_hb_standards_api(
    industries: Optional[List[str]] = Query(None, description="行业代码列表，如 AQ/XF"),
    max_results: int = 500,
    scan_only: bool = False,
    incr: bool = False,
    resume_task_id: Optional[str] = None,
    keyword_group: str = '安全生产',
    std_state: str = '现行',
):
    _check_max_results(keyword_group, max_results)
    if industries:
        industries = [_resolve_hb_industry(i) for i in industries]
        industries = [i for i in industries if i]

    config = {
        'max_results': max_results,
        'incr': incr,
        'keyword_group': keyword_group,
        'scan_only': scan_only,
        'industries': industries,
        'std_state': std_state,
    }
    return _create_scan_task('hb', 'hb', config, resume_task_id=resume_task_id)


@router.post("/api/scan/db")
async def scan_db_standards_api(
    provinces: Optional[List[str]] = Query(None, description="省份名称列表，如 江苏省/浙江省"),
    max_results: int = 500,
    scan_only: bool = False,
    incr: bool = False,
    resume_task_id: Optional[str] = None,
    keyword_group: str = '安全生产',
    std_state: str = '现行',
):
    _check_max_results(keyword_group, max_results)
    config = {
        'max_results': max_results,
        'incr': incr,
        'keyword_group': keyword_group,
        'scan_only': scan_only,
        'provinces': provinces,
        'std_state': std_state,
    }
    return _create_scan_task('db', 'db', config, resume_task_id=resume_task_id)


@router.post("/api/scan/tt")
async def scan_tt_standards_api(
    cnl1_codes: Optional[List[str]] = Query(None, description="中国标准分类号一级代码列表，如 S/A/M；不传=全量扫描"),
    max_results: int = 500,
    scan_only: bool = False,
    incr: bool = False,
    resume_task_id: Optional[str] = None,
    keyword_group: str = '安全生产',
    std_state: str = '现行',
):
    """团体标准扫描。直连 ttbz 官方 API，无需 Playwright/WAF。"""
    _check_max_results(keyword_group, max_results)
    config = {
        'max_results': max_results,
        'incr': incr,
        'keyword_group': keyword_group,
        'scan_only': scan_only,
        'cnl1_codes': cnl1_codes,
        'std_state': std_state,
    }
    return _create_scan_task('tt', 'tt', config, resume_task_id=resume_task_id)


@router.post("/api/scan/mem")
async def scan_mem_standards_api(
    source: str = Query('bz', description="数据源：bz=标准文本 | gz=规章 | all=两源并发扫描"),
    max_results: int = 500,
    scan_only: bool = False,
    incr: bool = False,
    resume_task_id: Optional[str] = None,
    keyword_group: str = '安全生产',
    std_state: str = '现行',
):
    """应急管理部标准/规章扫描。直连 mem.gov.cn 静态 HTML，无 WAF。

    - source='bz' 标准文本：mem.gov.cn 不提供状态字段，所有标准按"现行"处理
      ("现行"/"全部" 返回全部；"废止" 返回空)
    - source='gz' 规章：从标题识别"已废止"状态
    - source='all' 并发扫描 bz + gz 两源，合并结果
    """
    if source not in ('bz', 'gz', 'all'):
        raise HTTPException(status_code=400, detail="source 必须为 'bz'、'gz' 或 'all'")
    _check_max_results(keyword_group, max_results)
    config = {
        'max_results': max_results,
        'incr': incr,
        'keyword_group': keyword_group,
        'scan_only': scan_only,
        'std_state': std_state,
        'source': source,
    }
    return _create_scan_task('mem', 'mem', config, resume_task_id=resume_task_id)


@router.post("/api/scan/all")
async def scan_all_standards_api(body: ScanAllRequest):
    valid_types = {'gb', 'hb', 'db', 'tt', 'mem'}
    scan_types = [t for t in body.types if t in valid_types]
    if not scan_types:
        raise HTTPException(status_code=400, detail="types 参数必须包含 gb/hb/db/tt/mem 中的至少一个")
    _check_max_results(body.keyword_group, body.max_results)

    task_ids, scan_fn = create_combined_scan_tasks(
        scan_types=scan_types, max_results=body.max_results, incr=body.incr,
        keyword_group=body.keyword_group, scan_only=body.scan_only,
        std_state=body.std_state, allow_preview=body.allow_preview,
        gb_config=body.gb_config, hb_config=body.hb_config, db_config=body.db_config,
        tt_config=body.tt_config, mem_config=body.mem_config,
    )
    _launch_task(scan_fn(), "scan-combined")
    return {"success": True, "task_ids": task_ids}


def send_error_alert(task_id, error_message):
    """发送错误告警"""
    ns = get_notification_service()
    ns.send_message(
        "⚠️ 任务执行错误",
        f"任务 {task_id} 执行失败\n\n错误信息: {error_message}\n时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    _log.error(f"已发送错误告警: {task_id}")
