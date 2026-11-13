"""API routes — Logs

应用内日志查看：读取 ~/.std_scanner/logs 下的运行日志尾部，
避免用户为了排查问题再去手工找文件。
"""
import asyncio
import os
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from config.paths import LOG_DIR

router = APIRouter(prefix="", tags=["Logs"])

_LOG_GLOB = 'std_scraper_*.log'
_MAX_TAIL_LINES = 2000
_READ_BLOCK = 256 * 1024


def _log_files(log_dir=None):
    """按修改时间倒序列出日志文件（仅 std_scraper_*.log）。"""
    log_dir = Path(log_dir or LOG_DIR)
    try:
        files = [p for p in log_dir.glob(_LOG_GLOB) if p.is_file()]
        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []


def _resolve_log_file(name=None, log_dir=None):
    """解析文件名到日志目录内的实际路径；文件名不匹配时返回 None。"""
    files = _log_files(log_dir)
    if name:
        for path in files:
            if path.name == name:
                return path
        return None
    return files[0] if files else None


def _tail_lines(path, lines, max_lines=_MAX_TAIL_LINES):
    """从文件末尾读取最后 lines 行，按块回退避免整文件载入内存。"""
    lines = max(1, min(int(lines), max_lines))
    with open(path, 'rb') as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        block = min(size, _READ_BLOCK)
        data = b''
        while True:
            f.seek(size - block)
            data = f.read(block)
            if data.count(b'\n') > lines or block >= size:
                break
            block = min(size, block * 4)
    rows = data.decode('utf-8', errors='replace').splitlines()
    return rows[-lines:]


@router.get("/api/logs/files")
async def list_log_files_api():
    """列出可用日志文件（名称、大小、修改时间）。"""
    files = []
    for path in _log_files():
        try:
            st = path.stat()
        except OSError:
            continue
        files.append({"name": path.name, "size": st.st_size, "mtime": st.st_mtime})
    return {"success": True, "dir": str(LOG_DIR), "files": files}


@router.get("/api/logs/tail")
async def tail_log_api(
    lines: int = Query(200, ge=1, le=_MAX_TAIL_LINES),
    file: str = '',
):
    """读取日志尾部。

    file 不传时优先返回最近修改的日志文件；文件不存在返回空行并给出提示。
    """
    path = _resolve_log_file(file or None)
    if not path:
        return {"success": True, "file": '', "dir": str(LOG_DIR), "lines": [],
                "truncated": False,
                "message": '暂无日志文件（可在设置中开启“保存日志到文件”）'}
    try:
        rows = await asyncio.to_thread(_tail_lines, path, lines)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"读取日志失败: {e}")
    return {"success": True, "file": path.name, "dir": str(LOG_DIR),
            "lines": rows, "truncated": len(rows) >= lines,
            "message": '' if rows else '日志文件为空'}


@router.post("/api/logs/open-dir")
async def open_log_dir_api():
    """在系统文件管理器中打开日志目录。"""
    if not LOG_DIR.exists():
        raise HTTPException(status_code=404, detail="日志目录不存在")
    try:
        if sys.platform.startswith('win'):
            os.startfile(str(LOG_DIR))
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', str(LOG_DIR)])
        else:
            subprocess.Popen(['xdg-open', str(LOG_DIR)])
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"打开日志目录失败: {e}")
    return {"success": True, "dir": str(LOG_DIR)}
