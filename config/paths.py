"""
集中路径管理模块
所有持久化路径（配置/数据库/任务）统一在此定义。

规范：
  - 用户数据统一存储在 ~/.std_scanner/（与项目目录解耦）
  - 项目本地数据（checkpoint、扫描结果 JSON）保留在项目目录
"""
import logging
from pathlib import Path

_log = logging.getLogger('std_scraper')

# ==================== 用户数据路径 ====================
CONFIG_DIR = Path.home() / ".std_scanner"
DB_PATH = CONFIG_DIR / "std_scanner.db"
CONFIG_FILE = CONFIG_DIR / "config.json"
TASKS_FILE = CONFIG_DIR / "tasks.json"

# 统一增量扫描 checkpoint（支持 gb/hb/db 三种类型）
SCAN_CHECKPOINT_FILE = CONFIG_DIR / "scan_checkpoint.json"

# ==================== 项目本地路径 ====================
BASE_DIR = Path(__file__).parent.parent  # 项目根目录（config/ → 根）
# 运行时数据文件统一存到用户数据目录，避免污染项目根目录
DATA_FILE = CONFIG_DIR / "safety_full.json"   # 扫描结果 JSON 快照（CLI 模式用）
CKPT_FILE = CONFIG_DIR / "scan_ckpt.json"     # 旧版，保留兼容
STATIC_DIR = BASE_DIR / "static"        # 静态资源目录（图标/CSS/字体等）
UI_FILE = BASE_DIR / "ui.html"          # WebUI 单文件 SPA

# ==================== 运行时日志路径 ====================
LOG_DIR = CONFIG_DIR / "logs"           # 日志文件目录

# ==================== 旧路径（用于迁移） ====================
_OLD_CONFIG_DIR = BASE_DIR / ".std_scanner"


def migrate_old_data() -> bool:
    """
    自动迁移：如果旧路径（项目目录下 .std_scanner/）存在数据，
    且新路径（~/.std_scanner/）为空，则迁移。

    Returns:
        True 如果执行了迁移，False 如果无需迁移。
    """
    import shutil

    if not _OLD_CONFIG_DIR.exists():
        return False

    # 检查旧路径是否有实质数据（非仅有空 JSON）
    old_files = list(_OLD_CONFIG_DIR.glob("*"))
    has_data = any(
        f.stat().st_size > 4 or f.suffix == ".db"
        for f in old_files if f.is_file()
    )

    if not has_data:
        return False

    # 确保新目录存在
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # 逐个迁移文件（不覆盖已有数据）
    migrated = False
    for old_file in old_files:
        if old_file.name.endswith(".backup"):
            continue  # 跳过备份文件
        new_file = CONFIG_DIR / old_file.name
        if not new_file.exists() or new_file.stat().st_size < old_file.stat().st_size:
            shutil.copy2(old_file, new_file)
            migrated = True

    if migrated:
        _log.info(f"配置数据已从 {_OLD_CONFIG_DIR} 迁移到 {CONFIG_DIR}")
        # 重命名旧目录作为备份
        try:
            _OLD_CONFIG_DIR.rename(_OLD_CONFIG_DIR.with_suffix(_OLD_CONFIG_DIR.suffix + ".migrated"))
        except OSError:
            pass  # 重命名失败不阻塞

    return migrated
