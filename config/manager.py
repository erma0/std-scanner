"""
配置管理模块

职责：
  - 配置文件的加载/保存/验证/脱敏
  - 默认配置定义
"""
import copy
import json
import logging
from pathlib import Path

from config.paths import CONFIG_DIR, CONFIG_FILE, migrate_old_data

_log = logging.getLogger('std_scraper')

# 迁移标记：确保 migrate_old_data() 只执行一次
_migration_done = False

# ==================== 默认配置 ====================
DEFAULT_CONFIG = {
    "notifications": {
        "serverchan": {"enabled": False, "sckey": ""},
        "pushplus": {"enabled": False, "token": ""},
        "wecom": {"enabled": False, "webhook": ""},
        "dingtalk": {"enabled": False, "webhook": "", "secret": ""},
    },
    "download": {
        "output_dir": str(Path.home() / "Downloads" / "安全标准"),
        "existing_dirs": [],
        "duplicate_check_strategy": "early",
        "delay": 3.0,         # 请求间隔（秒）
        "max_retries": 3,
        "retry_delay": 2.0,
        "concurrent": 1,
        "max_network_retries": 2,
        "strategy": "full",
        "allow_preview": True,   # 允许浏览器预览拼接下载（需 playwright 已安装）
        "preview_quality": 0.6,  # 预览 PDF 缩放比例（0.3~1.0，值越大质量越高、文件越大）
    },
    "logging": {
        "level": "INFO",
        "save_to_file": True,
    },
    "tasks": {
        "auto_save": True,
        "save_interval": 10,
        "retention_hours": 168,     # 已完成/失败任务保留时长（小时），默认 7 天
        "max_tasks": 200,           # 任务总数上限，超出时清理最旧的
    },
    # 行业/省份默认为空，运行时由 keywords.py 的 PRESET_GROUP 填充
    "keyword_groups": {
        "安全生产": {
            "keywords": [],
            "excludes": [],
            "industries": [],
            "provinces": [],
        },
    },
}


# ==================== 配置文件操作 ====================

def load_config():
    """加载配置文件，与默认值深度合并"""
    global _migration_done
    if not _migration_done:
        migrate_old_data()  # 自动迁移旧路径数据（仅执行一次）
        _migration_done = True
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                config = json.load(f)
                return deep_merge(DEFAULT_CONFIG, config)
        except (json.JSONDecodeError, OSError, ValueError) as e:
            _log.warning(f"加载配置文件失败: {e}")
            return copy.deepcopy(DEFAULT_CONFIG)
    return copy.deepcopy(DEFAULT_CONFIG)


def save_config(config):
    """保存配置文件（原子写入，防止崩溃导致配置丢失）"""
    import tempfile
    import os
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(CONFIG_DIR), suffix='.tmp')
    try:
        with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, str(CONFIG_FILE))
    except Exception:
        try: os.unlink(tmp_path)
        except OSError: pass
        raise


def deep_merge(default, custom):
    """递归深度合并配置字典"""
    result = copy.deepcopy(default)
    for key, value in custom.items():
        if isinstance(value, dict) and key in result and isinstance(result[key], dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def validate_config(config):
    """
    验证配置文件的完整性和有效性
    返回 (is_valid, errors)
    """
    errors = []

    # 验证下载配置
    download = config.get("download", {})
    delay = download.get("delay", 3.0)
    if not isinstance(delay, (int, float)) or delay < 0:
        errors.append("下载延迟必须是非负数")

    max_retries = download.get("max_retries", 3)
    if not isinstance(max_retries, int) or max_retries < 0:
        errors.append("最大重试次数必须是非负整数")

    concurrent = download.get("concurrent", 1)
    if not isinstance(concurrent, int) or concurrent < 1:
        errors.append("并发数必须大于等于1")

    # 验证日志配置
    log_cfg = config.get("logging", {})
    log_level = log_cfg.get("level", "INFO")
    valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    if log_level not in valid_levels:
        errors.append(f"日志级别必须是以下之一: {', '.join(valid_levels)}")

    return len(errors) == 0, errors


def mask_sensitive_config(config):
    """
    遮盖配置中的敏感信息，用于 API 返回
    """
    masked = copy.deepcopy(config)
    notifications = masked.get("notifications", {})

    if "serverchan" in notifications and notifications["serverchan"].get("sckey"):
        sckey = notifications["serverchan"]["sckey"]
        if len(sckey) >= 8:
            notifications["serverchan"]["sckey"] = sckey[:4] + "*" * (len(sckey) - 8) + sckey[-4:]
        else:
            notifications["serverchan"]["sckey"] = "*" * len(sckey)

    if "pushplus" in notifications and notifications["pushplus"].get("token"):
        token = notifications["pushplus"]["token"]
        if len(token) >= 8:
            notifications["pushplus"]["token"] = token[:4] + "*" * (len(token) - 8) + token[-4:]
        else:
            notifications["pushplus"]["token"] = "*" * len(token)

    if "wecom" in notifications and notifications["wecom"].get("webhook"):
        webhook = notifications["wecom"]["webhook"]
        if len(webhook) > 16:
            notifications["wecom"]["webhook"] = webhook[:8] + "..." + webhook[-8:]
        else:
            notifications["wecom"]["webhook"] = "*" * len(webhook)

    if "dingtalk" in notifications:
        if notifications["dingtalk"].get("webhook"):
            webhook = notifications["dingtalk"]["webhook"]
            if len(webhook) > 16:
                notifications["dingtalk"]["webhook"] = webhook[:8] + "..." + webhook[-8:]
            else:
                notifications["dingtalk"]["webhook"] = "*" * len(webhook)
        if notifications["dingtalk"].get("secret"):
            secret = notifications["dingtalk"]["secret"]
            if len(secret) > 8:
                notifications["dingtalk"]["secret"] = secret[:4] + "*" * (len(secret) - 8) + secret[-4:]
            else:
                notifications["dingtalk"]["secret"] = "*" * len(secret)

    return masked
