"""scanner.checkpoint — 统一增量扫描 Checkpoint"""

import json
import time
import logging

from config.paths import CKPT_FILE, SCAN_CHECKPOINT_FILE
from app.helpers import atomic_write

_log = logging.getLogger('std_scraper')

_migrated = False


def _migrate_old_ckpt():
    """将旧的 scan_ckpt.json 迁移到新的统一 checkpoint 文件（仅执行一次）"""
    global _migrated
    if _migrated:
        return
    if CKPT_FILE.exists() and not SCAN_CHECKPOINT_FILE.exists():
        try:
            with open(CKPT_FILE, 'r') as f:
                old = json.load(f)
            new = {
                'gb': {
                    'first_id': old.get('lastId'),
                    'last_page': old.get('lastPage'),
                    'count': old.get('count'),
                    'updatedAt': old.get('updatedAt', ''),
                }
            }
            SCAN_CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(str(SCAN_CHECKPOINT_FILE), json.dumps(new, ensure_ascii=False, indent=2), mode='w')
            CKPT_FILE.rename(CKPT_FILE.with_suffix('.json.migrated'))
            _log.info("[CKPT] 已从旧 checkpoint 迁移")
        except Exception as e:
            _log.warning(f"[CKPT] 旧 checkpoint 迁移失败: {e}")
    _migrated = True


def load_scan_checkpoint():
    """加载统一增量 checkpoint"""
    _migrate_old_ckpt()
    try:
        with open(SCAN_CHECKPOINT_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_scan_checkpoint(data):
    """保存统一增量 checkpoint（原子写入）"""
    data['_updatedAt'] = time.strftime('%Y-%m-%d %H:%M:%S')
    SCAN_CHECKPOINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(str(SCAN_CHECKPOINT_FILE), json.dumps(data, ensure_ascii=False, indent=2), mode='w')


def get_incr_checkpoint(scan_type, item_key=None):
    """获取特定类型/条目的增量 checkpoint"""
    ckpt = load_scan_checkpoint()
    if item_key:
        return ckpt.get(scan_type, {}).get(item_key)
    return ckpt.get(scan_type)


def update_incr_checkpoint(scan_type, item_key, data):
    """更新特定类型/条目的增量 checkpoint"""
    data['updatedAt'] = time.strftime('%Y-%m-%d %H:%M:%S')
    ckpt = load_scan_checkpoint()
    if item_key:
        ckpt.setdefault(scan_type, {})[item_key] = data
    else:
        ckpt[scan_type] = data
    _save_scan_checkpoint(ckpt)


def reset_incr_checkpoint(scan_type=None, item_key=None):
    """重置增量 checkpoint

    - scan_type=None, item_key=None → 清空全部
    - scan_type='gb', item_key=None → 清空国标
    - scan_type='hb', item_key='AQ' → 清空行标-安全生产
    """
    if scan_type is None:
        _log.info("[CKPT] 重置全部 checkpoint")
        _save_scan_checkpoint({})
        return

    ckpt = load_scan_checkpoint()
    if item_key:
        if scan_type in ckpt and isinstance(ckpt[scan_type], dict) and item_key in ckpt[scan_type]:
            del ckpt[scan_type][item_key]
            if not ckpt[scan_type]:
                del ckpt[scan_type]
            _log.info(f"[CKPT] 重置 {scan_type}/{item_key} checkpoint")
        else:
            _log.info(f"[CKPT] {scan_type}/{item_key} 无 checkpoint，跳过")
    else:
        if scan_type in ckpt:
            del ckpt[scan_type]
            _log.info(f"[CKPT] 重置 {scan_type} 全部 checkpoint")
        else:
            _log.info(f"[CKPT] {scan_type} 无 checkpoint，跳过")

    _save_scan_checkpoint(ckpt)


def load_ckpt():
    """旧版 checkpoint 加载（保留兼容），优先读取新版"""
    ckpt = load_scan_checkpoint()
    gb_ckpt = ckpt.get('gb')
    if gb_ckpt:
        return {
            'lastPage': gb_ckpt.get('last_page', 1),
            'lastId': gb_ckpt.get('first_id'),
            'count': gb_ckpt.get('count', 0),
        }
    return None


def save_ckpt(data):
    """旧版 checkpoint 保存（保留兼容），写入新版格式"""
    update_incr_checkpoint('gb', None, {
        'first_id': data.get('lastId'),
        'last_page': data.get('lastPage', 0),
        'count': data.get('count', 0),
    })
