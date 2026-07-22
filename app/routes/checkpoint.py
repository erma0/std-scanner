"""API routes — Checkpoint 管理"""

import copy
import logging

from fastapi import APIRouter, HTTPException, Query

from app.scanner.checkpoint import load_scan_checkpoint, reset_incr_checkpoint
from config.settings import HB_CODE_MAP
from .state import task_manager

_log = logging.getLogger('std_scraper')

router = APIRouter(prefix="/api/checkpoint", tags=["Checkpoint"])


def _check_scan_running():
    """检查是否有正在运行的扫描任务"""
    if not task_manager:
        return False
    try:
        running = task_manager.get_all(status_filter='running')
        return any(t.get('std_type') in ('gb', 'hb', 'db', 'tt') for t in running)
    except Exception as e:
        _log.debug(f"检查扫描任务状态失败，按无运行处理: {e}")
        return False


def _enrich_checkpoint(ckpt):
    """为 checkpoint 数据注入显示名等前端所需字段"""
    result = copy.deepcopy({k: v for k, v in ckpt.items() if not k.startswith('_')})

    if 'hb' in result and isinstance(result['hb'], dict):
        reverse_map = {v: k for k, v in HB_CODE_MAP.items()}
        for key, data in result['hb'].items():
            if isinstance(data, dict):
                code = reverse_map.get(key, key)
                data['label'] = HB_CODE_MAP.get(code, key)

    return result


@router.get("")
async def get_checkpoint():
    """获取全部增量 checkpoint 状态"""
    ckpt = load_scan_checkpoint()
    return _enrich_checkpoint(ckpt)


@router.delete("")
async def reset_checkpoint(
    scan_type: str = Query(None, description="gb|hb|db|tt，不传则重置全部"),
    item: str = Query(None, description="行业代码或省份，仅 hb/db 有效"),
):
    """重置增量 checkpoint

    调用示例：
    - DELETE /api/checkpoint → 重置全部
    - DELETE /api/checkpoint?scan_type=gb → 重置国标
    - DELETE /api/checkpoint?scan_type=hb → 重置全部行标
    - DELETE /api/checkpoint?scan_type=hb&item=AQ → 重置行标-安全生产
    - DELETE /api/checkpoint?scan_type=tt → 重置团标
    """
    if _check_scan_running():
        raise HTTPException(status_code=409, detail="有扫描任务正在执行，请等待完成后再重置")

    if scan_type is not None and scan_type not in ('gb', 'hb', 'db', 'tt'):
        raise HTTPException(status_code=400, detail=f"无效的扫描类型: {scan_type}")

    if scan_type in ('gb', 'tt') and item:
        raise HTTPException(status_code=400, detail=f"{scan_type.upper()} 不支持 item 参数")

    ckpt = load_scan_checkpoint()
    deleted_summary = None

    if scan_type is None:
        deleted_summary = {"types": [k for k in ckpt.keys() if not k.startswith('_')]}
        reset_incr_checkpoint()
    elif item:
        item_data = ckpt.get(scan_type, {}).get(item) if isinstance(ckpt.get(scan_type), dict) else None
        deleted_summary = {
            "item": f"{scan_type}/{item}",
            "count": item_data.get('count', 0) if isinstance(item_data, dict) else 0,
        }
        reset_incr_checkpoint(scan_type, item)
    else:
        type_data = ckpt.get(scan_type)
        if isinstance(type_data, dict) and all(isinstance(v, dict) for v in type_data.values()):
            deleted_summary = {"type": scan_type, "items": list(type_data.keys())}
        else:
            deleted_summary = {"type": scan_type}
        reset_incr_checkpoint(scan_type, None)

    return {"success": True, "action": "reset", "deleted": deleted_summary}
