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


def invalidate_existing_dirs_cache():
    """使去重目录缓存失效"""
    global _existing_dirs_cache
    _existing_dirs_cache = None


def get_existing_dirs() -> list:
    """从配置获取用于去重检查的本地文件夹列表"""
    global _existing_dirs_cache
    if _existing_dirs_cache is not None:
        return _existing_dirs_cache

    try:
        config = load_config()
        dirs = config.get('download', {}).get('existing_dirs', [])
        _existing_dirs_cache = [d for d in dirs if d and os.path.isdir(d)]
    except Exception:
        _existing_dirs_cache = []

    return _existing_dirs_cache


# ==================== 递归扫描 ====================
def _scandir_recursive(path: str) -> set:
    """递归扫描目录，返回所有 .pdf 文件名集合"""
    result = set()
    try:
        with os.scandir(path) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False):
                    result.update(_scandir_recursive(entry.path))
                elif entry.is_file(follow_symlinks=False) and entry.name.endswith('.pdf'):
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


def _need_rescan() -> bool:
    """判断是否需要重新扫描（600s 缓存过期 或 文件夹 mtime 变化）"""
    global _existing_files_last_scan
    if time.time() - _existing_files_last_scan > 600:
        return True
    dirs = [str(get_output_dir())] + get_existing_dirs()
    for d in dirs:
        old_mtime = _existing_files_mtime.get(d, 0)
        new_mtime = _get_folder_mtime(d)
        if new_mtime > old_mtime:
            return True
    return False


def _scan_single_dir(path: str) -> tuple:
    """扫描单个目录，返回 (文件集合, 文件数量)"""
    if not path or not os.path.isdir(path):
        return set(), 0
    try:
        files = _scandir_recursive(path)
        return files, len(files)
    except Exception:
        return set(), 0


def get_existing_files(force_refresh: bool = False) -> set:
    """
    获取所有已有 PDF 文件名集合（用于去重）。
    600s 缓存窗口，通过文件夹 mtime 判断是否需重扫。
    """
    global _existing_files_cache, _existing_files_last_scan, _existing_files_mtime

    if _existing_files_cache is not None and not force_refresh and not _need_rescan():
        return _existing_files_cache

    existing = set()
    dirs = [str(get_output_dir())] + get_existing_dirs()

    _log.info("正在扫描文件夹用于去重检查...")
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=min(len(dirs), 4)) as executor:
        futures = {executor.submit(_scan_single_dir, d): d for d in dirs if d}
        for fut in futures:
            files, count = fut.result()
            existing.update(files)
            if count > 0:
                _log.debug(f"  扫描: {futures[fut]} → {count} 个 PDF")

    with _cache_lock:
        _existing_files_cache = existing
        _existing_files_last_scan = time.time()
        for d in dirs:
            if d and os.path.isdir(d):
                _existing_files_mtime[d] = _get_folder_mtime(d)

    elapsed = time.time() - start_time
    _log.info(f"去重扫描完成: {len(existing)} 个 PDF (耗时 {elapsed:.1f}s)")
    return existing


def add_to_existing_files_cache(filename: str):
    """向缓存中添加新下载的文件名"""
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
                            if fname.endswith('.pdf'):
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
    if _file_watcher_stop:
        _file_watcher_stop.set()
    _file_watcher_thread = None
    _log.info("文件监控已停止")


def get_dedup_stats() -> dict:
    """获取去重系统状态（供 health API 使用）"""
    return {
        "cached_files": len(_existing_files_cache) if _existing_files_cache else 0,
        "last_scan_ago_seconds": round(time.time() - _existing_files_last_scan, 1) if _existing_files_last_scan else None,
        "file_watcher_active": _file_watcher_thread is not None and _file_watcher_thread.is_alive(),
    }
