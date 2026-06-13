"""API routes — Config"""
import logging as _logging_mod
from datetime import datetime
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .state import task_manager, ns
from config.manager import load_config, save_config, deep_merge, validate_config, mask_sensitive_config
from config.settings import HB_CODE_MAP, HB_SAFETY_CODES
from app.keywords import load_keywords, get_all_groups, save_groups, delete_group, reset_to_default, import_to_group
from app.notifier import get_notification_service, reset_notification_service

_log = _logging_mod.getLogger('std_scraper')


class ImportKeywordsRequest(BaseModel):
    group: str = Field(default="安全生产", description="目标关键词组名")
    text: str = Field(..., min_length=1, description="导入文本")


class NotificationConfigUpdate(BaseModel):
    serverchan: Optional[dict] = None
    pushplus: Optional[dict] = None
    wecom: Optional[dict] = None
    dingtalk: Optional[dict] = None


router = APIRouter(prefix="", tags=["Config"])


@router.get("/api/config")
async def get_config_api():
    """获取配置（遮盖敏感信息）"""
    return mask_sensitive_config(load_config())


@router.put("/api/config")
async def update_config_api(new_config: Dict):
    """更新配置（支持热更新）"""
    full_config = load_config()
    full_config = deep_merge(full_config, new_config)

    is_valid, errors = validate_config(full_config)
    if not is_valid:
        raise HTTPException(status_code=400, detail={"errors": errors})

    save_config(full_config)

    reset_notification_service()

    if "logging" in new_config:
        log_level = _logging_mod.getLevelName(full_config['logging'].get('level', 'INFO'))
        if not isinstance(log_level, int):
            log_level = _logging_mod.INFO
        for handler in _log.handlers:
            handler.setLevel(log_level)
        _log.setLevel(log_level)
        _log.info(f"日志级别已更新为: {full_config['logging'].get('level', 'INFO')}")

    _log.info("配置已更新")
    return {"success": True, "config": mask_sensitive_config(full_config)}


@router.get("/api/industries")
async def get_industries():
    """获取所有行业代码和名称"""
    return {
        "safety_codes": HB_SAFETY_CODES,
        "safety_industries": [HB_CODE_MAP[c] for c in HB_SAFETY_CODES],
        "all_codes": list(HB_CODE_MAP.keys()),
        "all_industries": list(HB_CODE_MAP.values()),
        "code_map": HB_CODE_MAP
    }


@router.get("/api/keyword_groups")
async def get_keyword_groups():
    """获取所有关键词组（含 keywords/excludes/industries/provinces）"""
    groups = get_all_groups()
    summary = {}
    for name, grp in groups.items():
        if isinstance(grp, dict):
            summary[name] = {
                'keywords': len(grp.get('keywords', [])),
                'excludes': len(grp.get('excludes', [])),
                'industries': len(grp.get('industries', [])),
                'provinces': len(grp.get('provinces', [])),
            }
        else:
            summary[name] = {'keywords': len(grp) if isinstance(grp, list) else 0}
    return {"groups": groups, "summary": summary, "count": len(groups)}


@router.put("/api/keyword_groups")
async def update_keyword_groups(request: Request):
    """保存所有关键词组（全量覆盖）"""
    body = await request.json()
    groups = body.get("groups", {})
    if not isinstance(groups, dict) or not groups:
        raise HTTPException(status_code=400, detail="关键词组不能为空")
    save_groups(groups)
    return {"success": True, "count": len(groups)}


@router.post("/api/keyword_groups/import")
async def import_keywords(body: ImportKeywordsRequest):
    """批量导入关键词到指定组"""
    result = import_to_group(body.group, body.text)
    return {"success": True, **result}


@router.delete("/api/keyword_groups/{name}")
async def delete_keyword_group(name: str):
    """删除一个关键词组"""
    ok = delete_group(name)
    if not ok:
        raise HTTPException(status_code=400, detail=f"无法删除组 '{name}'")
    return {"success": True}


@router.post("/api/keyword_groups/reset")
async def reset_keyword_groups():
    """重置为默认（仅保留默认组，使用内置关键词）"""
    reset_to_default()
    return {"success": True}


@router.get("/api/keywords")
async def get_keywords_api():
    """获取当前关键词列表"""
    return {"keywords": load_keywords()}


@router.put("/api/keywords")
async def update_keywords_api(request: Request):
    """保存自定义关键词列表（兼容旧 API，保存到安全生产组的 keywords 字段）"""
    body = await request.json()
    keywords = body.get("keywords", [])
    if not isinstance(keywords, list) or not keywords:
        raise HTTPException(status_code=400, detail="关键词列表不能为空")
    groups = get_all_groups()
    grp = groups.get('安全生产', {})
    if isinstance(grp, dict):
        grp['keywords'] = keywords
        groups['安全生产'] = grp
    else:
        groups['安全生产'] = {'keywords': keywords, 'excludes': [], 'industries': [], 'provinces': []}
    save_groups(groups)
    return {"success": True, "count": len(keywords)}


@router.post("/api/keywords/reload")
async def reload_keywords_api():
    """重新加载关键词（兼容旧 API）"""
    groups = get_all_groups()
    grp = groups.get('安全生产', [])
    kws = grp.get('keywords', grp) if isinstance(grp, dict) else (grp if isinstance(grp, list) else [])
    if isinstance(kws, dict):
        kws = kws.get('keywords', [])
    return {"success": True, "keywords": kws, "count": len(kws)}


@router.post("/api/keywords/reset")
async def reset_keywords_api():
    """重置为内置默认关键词（兼容旧 API）"""
    reset_to_default()
    grp = get_all_groups().get('安全生产', [])
    kws = grp.get('keywords', grp) if isinstance(grp, dict) else (grp if isinstance(grp, list) else [])
    if isinstance(kws, dict):
        kws = kws.get('keywords', [])
    return {"success": True, "keywords": kws, "count": len(kws)}


@router.post("/api/config/notifications")
async def update_notification_config(notif_config: NotificationConfigUpdate):
    """更新通知配置"""
    full_config = load_config()
    update_dict = notif_config.model_dump(exclude_none=True)
    if "notifications" in full_config:
        full_config["notifications"] = deep_merge(full_config["notifications"], update_dict)
    else:
        full_config["notifications"] = update_dict
    save_config(full_config)
    reset_notification_service()
    _log.info("通知配置已更新")
    return {"success": True}


@router.post("/api/test_notification")
async def test_notification_api():
    """测试通知配置"""
    ns = get_notification_service()
    test_report = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "scanned": 100,
        "downloaded": 95,
        "success": 90,
        "failed": 5,
        "skipped": 5,
        "message": "这是一条测试通知"
    }
    results = ns.send_report(test_report)
    _log.info(f"测试通知已发送: {results}")
    all_success = all(results.values()) if results else False
    return {"success": all_success, "results": results}


@router.get("/api/playwright_status")
async def playwright_status():
    """检查 playwright 是否已安装并可用"""
    from app.scanner.preview import PLAYWRIGHT_AVAILABLE
    return {"available": PLAYWRIGHT_AVAILABLE}
