"""API routes — Files"""
import os
import asyncio
import threading
import logging
from datetime import datetime
from pathlib import Path
from typing import List

from fastapi import APIRouter, Request

from . import state as _state
from config.manager import load_config, save_config
from config.settings import get_output_dir
# app.dedup 含 watchfiles（~0.5s），延迟到 invalidate_existing_dirs_cache 首次调用时
from app.helpers import validate_path

_log = logging.getLogger('std_scraper')


router = APIRouter(prefix="", tags=["Files"])


@router.get("/api/output_dir")
async def get_output_dir_api():
    """获取输出目录"""
    cfg = load_config()
    return {
        "output_dir": cfg.get('download', {}).get('output_dir', str(get_output_dir())),
        "existing_dirs": cfg.get('download', {}).get('existing_dirs', [])
    }


@router.post("/api/open_output_dir")
async def open_output_dir_api():
    """打开输出目录"""
    cfg = load_config()
    output_dir = cfg.get('download', {}).get('output_dir', str(get_output_dir()))

    validated_path = validate_path(output_dir)
    if not validated_path:
        return {"success": False, "error": "无效的输出目录路径"}

    path = Path(validated_path)
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
    try:
        os.startfile(validated_path)
        return {"success": True, "output_dir": validated_path}
    except Exception as e:
        _log.error(f"打开文件夹失败: {e}")
        return {"success": False, "error": str(e)}


@router.post("/api/open_file")
async def open_file_api(request: Request):
    """用系统默认程序打开文件"""
    body = await request.json()
    filepath = body.get("path")
    if not filepath:
        return {"success": False, "error": "未提供文件路径"}

    validated_path = validate_path(filepath, base_dir=str(get_output_dir()))
    if not validated_path:
        return {"success": False, "error": "无效的文件路径"}

    try:
        os.startfile(validated_path)
        return {"success": True, "path": validated_path}
    except Exception as e:
        _log.error(f"打开文件失败: {e}")
        return {"success": False, "error": str(e)}


@router.post("/api/open_url")
async def open_url_api(request: Request):
    """用系统默认浏览器打开 URL"""
    body = await request.json()
    url = body.get("url")
    if not url:
        return {"success": False, "error": "未提供 URL"}
    if not url.startswith(('http://', 'https://')):
        return {"success": False, "error": "只允许打开 HTTP/HTTPS 链接"}
    try:
        import webbrowser
        webbrowser.open(url)
        return {"success": True, "url": url}
    except Exception as e:
        _log.error(f"打开 URL 失败: {e}")
        return {"success": False, "error": str(e)}


@router.post("/api/select_folder")
async def select_folder_api():
    """通过 pywebview 弹出系统文件夹选择对话框"""
    if not _state.pywebview_window:
        return {"success": False, "error": "pywebview 窗口未初始化，请在桌面应用中运行"}
    import webview

    loop = asyncio.get_running_loop()
    future = asyncio.Future()

    def _show_dialog():
        try:
            result = _state.pywebview_window.create_file_dialog(
                webview.FileDialog.FOLDER,
                directory=""
            )
            loop.call_soon_threadsafe(
                lambda: future.set_result(result[0] if result else None)
            )
        except Exception as exc:  # noqa: F841
            loop.call_soon_threadsafe(
                lambda: future.set_exception(exc)  # noqa: F821
            )

    threading.Thread(target=_show_dialog, daemon=True).start()
    folder = await future

    if folder:
        return {"success": True, "path": folder}
    return {"success": False, "path": None}


@router.post("/api/save_file_dialog")
async def save_file_dialog_api(request: Request):
    """通过 pywebview 弹出系统文件保存对话框"""
    if not _state.pywebview_window:
        return {"success": False, "error": "pywebview 窗口未初始化，请在桌面应用中运行"}
    import webview

    body = await request.json()
    default_name = body.get("default_name", "")
    file_types = body.get("file_types", [])

    loop = asyncio.get_running_loop()
    future = asyncio.Future()

    def _show_dialog():
        try:
            result = _state.pywebview_window.create_file_dialog(
                webview.FileDialog.SAVE,
                directory="",
                save_filename=default_name,
                file_types=file_types
            )
            loop.call_soon_threadsafe(
                lambda: future.set_result(result[0] if result else None)
            )
        except Exception as exc:  # noqa: F841
            loop.call_soon_threadsafe(
                lambda: future.set_exception(exc)  # noqa: F821
            )

    threading.Thread(target=_show_dialog, daemon=True).start()
    filepath = await future

    if filepath:
        return {"success": True, "path": filepath}
    return {"success": False, "path": None}


@router.get("/api/browse_folder")
async def browse_folder_api(path: str = ""):
    """浏览文件夹，返回指定路径下的目录和 PDF 文件列表"""
    if not path:
        path = str(get_output_dir())
    validated = validate_path(path)
    if not validated:
        return {"success": False, "error": "无效的路径", "path": path}
    target = Path(validated)
    if not target.exists():
        target = Path(str(get_output_dir()))
        if not target.exists():
            return {"success": False, "error": "路径不存在", "path": path}

    try:
        items = []
        for entry in sorted(os.scandir(str(target)), key=lambda e: (not e.is_dir(), e.name.lower())):
            item = {
                "name": entry.name,
                "is_dir": entry.is_dir(),
                "path": str(Path(entry.path).resolve()),
            }
            if entry.is_file():
                stat = entry.stat()
                item["size"] = stat.st_size
                item["modified"] = datetime.fromtimestamp(stat.st_mtime).isoformat()
            items.append(item)

        parent = str(target.parent.resolve()) if target.parent != target else str(target)
        return {
            "success": True,
            "path": str(target.resolve()),
            "parent": parent,
            "items": items
        }
    except PermissionError:
        return {"success": False, "error": "没有权限访问该目录", "path": str(target)}
    except Exception as e:
        return {"success": False, "error": str(e), "path": str(target)}


@router.get("/api/existing_dirs")
async def get_existing_dirs_api():
    """获取用于去重检查的本地文件夹列表"""
    cfg = load_config()
    existing_dirs = cfg.get('download', {}).get('existing_dirs', [])
    validated_dirs = []
    for d in existing_dirs:
        validated = {
            "path": d,
            "exists": os.path.isdir(d),
            "file_count": 0
        }
        if validated["exists"]:
            try:
                validated["file_count"] = len([f for f in os.listdir(d) if f.endswith('.pdf')])
            except OSError:
                pass
        validated_dirs.append(validated)

    return {"existing_dirs": validated_dirs}


@router.post("/api/existing_dirs")
async def update_existing_dirs_api(dirs: List[str]):
    """更新用于去重检查的本地文件夹列表"""
    cfg = load_config()
    if 'download' not in cfg:
        cfg['download'] = {}
    cfg['download']['existing_dirs'] = dirs
    save_config(cfg)
    from app.dedup import invalidate_existing_dirs_cache
    invalidate_existing_dirs_cache()
    return {"success": True, "existing_dirs": dirs}
