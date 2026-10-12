"""
文件去重与实时监控模块
三层策略：os.scandir 递归扫描 + 多线程并行 + watchfiles 实时监控
"""
import os
import time
import threading
import logging
from concurrent.futures import ThreadPoolExecutor

_log = logging.getLogger('std_scraper')

from config.settings import get_output_dir
from config.manager import load_config
from app.helpers import safe_filename

# ==================== 常量 ====================
_CACHE_TTL_SECONDS = 600  # 去重缓存窗口（秒）
# 去重识别的文件后缀：按文件名主干跨格式匹配，同一标准的不同格式（PDF/Word）视为已存在
_DEDUP_EXTENSIONS = ('.pdf', '.doc', '.docx')


# ==================== 匹配键 ====================
def dedup_key(filename: str) -> str:
    """计算去重匹配键：文件名归一化（safe_filename）后取扩展名前的主干并小写。

    生成与扫描共用同一口径，因此：
    1. 全角/半角、空格、横杠、括号等历史命名差异不会造成重复下载；
    2. 同一标准的主干相同，跨格式（.pdf/.doc/.docx）视为同一份。
    """
    stem = os.path.splitext(safe_filename(filename))[0]
    return stem.strip().lower()


class ExistingFilesSet(set):
    """去重文件名集合：内部只存归一化主干键，成员判断自动按主干命中。"""

    def __contains__(self, filename):
        return set.__contains__(self, dedup_key(filename))

    def add(self, filename):
        set.add(self, dedup_key(filename))

    def update(self, filenames):
        for name in filenames:
            self.add(name)


# ==================== 缓存状态 ====================
_existing_files_cache = None
_existing_files_mtime = {}
_existing_files_last_scan = 0
_cache_lock = threading.Lock()

# ==================== 文件监控 ====================
_file_watcher_thread = None
_file_watcher_stop = None

# ==================== 去重目录缓存 ====================
_existing_dirs_cache = None
_existing_dirs_lock = threading.Lock()
_existing_dirs_counts = {}  # {目录: 递归扫描到的文档数}，与去重扫描同源


def invalidate_existing_dirs_cache():
    """使去重目录缓存失效"""
    global _existing_dirs_cache
    with _existing_dirs_lock:
        _existing_dirs_cache = None


def get_existing_dirs() -> list:
    """从配置获取用于去重检查的本地文件夹列表"""
    global _existing_dirs_cache
    with _existing_dirs_lock:
        if _existing_dirs_cache is not None:
            return _existing_dirs_cache

    try:
        config = load_config()
        dirs = config.get('download', {}).get('existing_dirs', [])
        result = [d for d in dirs if d and os.path.isdir(d)]
    except Exception as e:
        _log.warning(f"加载去重目录配置失败: {e}")
        result = []

    with _existing_dirs_lock:
        _existing_dirs_cache = result
    return result


# ==================== 递归扫描 ====================
def _scandir_recursive(path: str) -> set:
    """递归扫描目录，返回所有支持去重的文件名集合（含后缀，原始大小写）。

    匹配键（归一化主干、小写、跨格式）由 ExistingFilesSet/dedup_key 统一处理，
    这里只负责按 _DEDUP_EXTENSIONS 过滤与递归收集。
    """
    result = set()
    try:
        with os.scandir(path) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    result.update(_scandir_recursive(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    _, ext = os.path.splitext(entry.name)
                    if ext.lower() in _DEDUP_EXTENSIONS:
                        result.add(entry.name)
    except (PermissionError, OSError):
        pass
    return result


def _get_folder_mtime(path: str) -> float:
    """获取文件夹修改时间"""
    if not path or not os.path.isdir(path):
        return 0
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def _scan_single_dir(path: str) -> tuple:
    """扫描单个目录，返回 (文件集合, 文件数量)"""
    if not path or not os.path.isdir(path):
        return set(), 0
    try:
        files = _scandir_recursive(path)
        return files, len(files)
    except Exception as e:
        _log.debug(f"扫描目录失败 {path}: {e}")
        return set(), 0


def get_existing_files(force_refresh: bool = False) -> set:
    """
    获取所有已存在的标准集合（按归一化主干去重，PDF/Word 跨格式合并）。
    缓存窗口由 _CACHE_TTL_SECONDS 控制，通过文件夹 mtime 判断是否需重扫。
    集合成员为主干键：调用方直接用完整文件名（含后缀）判断即可命中。
    """
    global _existing_files_cache, _existing_files_last_scan, _existing_files_mtime, _existing_dirs_counts

    # 快速路径：缓存有效直接返回快照
    with _cache_lock:
        if _existing_files_cache is not None and not force_refresh:
            last_scan = _existing_files_last_scan
            mtimes = dict(_existing_files_mtime)
            cached = _existing_files_cache
        else:
            cached = None
            last_scan = 0
            mtimes = {}
    if cached is not None and time.time() - last_scan <= _CACHE_TTL_SECONDS:
        need = False
        for d in [str(get_output_dir())] + get_existing_dirs():
            if _get_folder_mtime(d) > mtimes.get(d, 0):
                need = True
                break
        if not need:
            return cached

    existing = ExistingFilesSet()
    dirs = [str(get_output_dir())] + get_existing_dirs()
    counts = {}

    _log.info("正在扫描文件夹用于去重检查...")
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=min(len(dirs), 4)) as executor:
        futures = {executor.submit(_scan_single_dir, d): d for d in dirs if d}
        for fut in futures:
            files, count = fut.result()
            existing.update(files)
            counts[futures[fut]] = count
            if count > 0:
                _log.debug(f"  扫描: {futures[fut]} → {count} 个文档")

    with _cache_lock:
        _existing_files_cache = existing
        _existing_files_last_scan = time.time()
        _existing_files_mtime = {d: _get_folder_mtime(d) for d in dirs if d and os.path.isdir(d)}
        _existing_dirs_counts = counts

    elapsed = time.time() - start_time
    _log.info(f"去重扫描完成: {len(existing)} 个标准 (耗时 {elapsed:.1f}s)")
    return existing


def get_existing_dir_counts() -> dict:
    """返回各去重目录递归扫描到的文档数量（口径与去重扫描一致，必要时触发一次扫描）"""
    get_existing_files()
    with _cache_lock:
        return dict(_existing_dirs_counts)


def add_to_existing_files_cache(filename: str):
    """向缓存中添加新下载的文件名（按归一化主干键，与扫描保持一致）"""
    global _existing_files_cache
    with _cache_lock:
        if _existing_files_cache is not None:
            _existing_files_cache.add(filename)


# ==================== watchfiles 实时监控 ====================
def start_file_watcher():
    """启动实时文件监控（watchfiles）"""
    global _file_watcher_thread, _file_watcher_stop

    if _file_watcher_thread is not None:
        return

    try:
        from watchfiles import watch
    except ImportError:
        _log.debug("watchfiles 未安装，跳过文件监控")
        return

    _file_watcher_stop = threading.Event()

    def _watch():
        dirs = [str(get_output_dir())] + get_existing_dirs()
        dirs = [d for d in dirs if d and os.path.isdir(d)]
        if not dirs:
            return

        try:
            for changes in watch(*dirs, rust_timeout=1000, yield_on_timeout=True):
                if _file_watcher_stop.is_set():
                    break
                if not changes:
                    continue
                with _cache_lock:
                    if _existing_files_cache is not None:
                        for change in changes:
                            fname = os.path.basename(change[1])
                            _, ext = os.path.splitext(fname)
                            if ext.lower() in _DEDUP_EXTENSIONS:
                                _existing_files_cache.add(fname)
        except Exception as e:
            _log.warning(f"文件监控异常退出: {e}")
        finally:
            global _file_watcher_thread
            _file_watcher_thread = None

    _file_watcher_thread = threading.Thread(target=_watch, daemon=True)
    _file_watcher_thread.start()
    _log.info("文件监控已启动")


def stop_file_watcher():
    """停止文件监控"""
    global _file_watcher_thread, _file_watcher_stop
    stop_event = _file_watcher_stop
    thread = _file_watcher_thread
    if stop_event:
        stop_event.set()
    if thread is not None and thread.is_alive():
        thread.join(timeout=2)
    _file_watcher_thread = None
    _log.info("文件监控已停止")


def get_dedup_stats() -> dict:
    """获取去重系统状态（供 health API 使用）"""
    with _cache_lock:
        cached_count = len(_existing_files_cache) if _existing_files_cache else 0
        last_scan = _existing_files_last_scan
    watcher_thread = _file_watcher_thread
    return {
        "cached_files": cached_count,
        "last_scan_ago_seconds": round(time.time() - last_scan, 1) if last_scan else None,
        "file_watcher_active": watcher_thread is not None and watcher_thread.is_alive(),
    }
