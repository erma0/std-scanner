"""API routes — Search"""
from fastapi import APIRouter, HTTPException, Request
import time
import asyncio
import logging
import re

from app.helpers import atomic_write

from .state import task_manager
from ._utils import launch_task as _launch_task, update_task_status
from app.scanner import (
    download_with_captcha, download_hb_with_captcha,
    fetch_stdpage_search, check_downloadable,
    check_hb_downloadable, get_detail_url_by_tid,
    preview_to_pdf, launch_browser,
    make_filename,
)
from app.scanner.gb_scan import _RE_XZ_BTN, _RE_CK_BTN
from app.dedup import get_existing_files, add_to_existing_files_cache
from config.settings import OPENSTD, DETAIL_URL, get_output_dir, DELAY, http_client

_log = logging.getLogger('std_scraper')


router = APIRouter(prefix="", tags=["Search"])

@router.post("/api/search/query")
async def search_query_api(request: Request):
    body = await request.json()
    query = body.get("query", "")
    std_type = body.get("std_type", "全部")
    max_results = body.get("max_results", 20)

    if not query:
        raise HTTPException(status_code=400, detail="搜索关键词不能为空")

    try:
        loop = asyncio.get_running_loop()
        standards, total = await loop.run_in_executor(
            None, fetch_stdpage_search, query, 1, std_type)
    except Exception as e:
        _log.error(f"搜索失败: {e}")
        raise HTTPException(status_code=500, detail=f"搜索失败: {e}")

    results = []
    for s in standards[:max_results]:
        can_dl, dl_msg = await loop.run_in_executor(
            None, check_downloadable, s['tid'])
        results.append({
            "code": s.get('code', ''),
            "name": s.get('name', ''),
            "tid": s.get('tid', ''),
            "pid": s.get('pid', ''),
            "status": s.get('status', ''),
            "publishDate": s.get('publishDate', ''),
            "actDate": s.get('actDate', ''),
            "downloadable": can_dl,
            "download_msg": dl_msg,
            "detailUrl": f"{DETAIL_URL}?id={s.get('tid', '')}",
        })

    return {"success": True, "total": total, "results": results}


@router.post("/api/search/download")
async def search_download_selected_api(request: Request):
    body = await request.json()
    items = body.get("items", [])

    if not items:
        raise HTTPException(status_code=400, detail="未选择任何标准")

    task_id = f"search_{int(time.time())}"

    task_manager.create(task_id, {
        "task_id": task_id,
        "status": "running",
        "progress": 0,
        "message": f"正在下载 {len(items)} 个标准",
        "stats": {"downloaded": 0, "success": 0, "failed": 0, "skipped": 0},
        "start_time": time.time(),
        "std_type": "search",
        "std_items": []
    })

    async def download_task():
        try:
            _log.info(f"任务 {task_id}: 下载选中标准 {len(items)} 个")
            await _download_selected_items(task_id, items)

            update_task_status(task_id, progress=100, status="completed",
                             message=f"下载完成，共处理 {len(items)} 个标准")
            task_manager.update(task_id, end_time=time.time())
            _log.info(f"任务 {task_id}: 完成")
        except Exception as e:
            _log.error(f"任务 {task_id} 异常: {e}")
            task_manager.update(task_id, status="failed", message=str(e), end_time=time.time())

    _launch_task(download_task(), f"download-{task_id}")
    return {"success": True, "task_id": task_id}


async def _download_selected_items(task_id, items):
    existing = get_existing_files()
    total = len(items)
    
    # 浏览器仅预览时按需启动，多个预览项复用同一个浏览器
    browser_ctx = None
    playwright_mgr = None

    try:
        for i, item in enumerate(items):
            code = item.get('code', '')
            name = item.get('name', '')
            pid = item.get('pid', '')
            tid = item.get('tid', '')

            _log.info(f"[{i+1}/{total}] 处理: {code} {name}")
            update_task_status(task_id,
                            progress=int((i / total) * 100),
                            message=f"[{i+1}/{total}] 处理: {code} {name}")

            filename = make_filename(code, name)
            if filename in existing:
                _log.info(f"  [SKIP] 已存在: {filename}")
                task_manager.increment_stats(task_id, skipped=1)
                continue

            can_dl, dl_msg = await asyncio.get_running_loop().run_in_executor(
                None, check_downloadable, tid)
            if not can_dl:
                _log.info(f"  [INFO] {dl_msg}")
                task_manager.increment_stats(task_id, skipped=1)
                continue

            detail_url, _ = await asyncio.get_running_loop().run_in_executor(
                None, get_detail_url_by_tid, tid, pid)

            if tid in ('BV_HB', 'BV_DB'):
                try:
                    can_download, hb_hash, pattern = await asyncio.get_running_loop().run_in_executor(
                        None, check_hb_downloadable, detail_url)
                    if not can_download or not hb_hash:
                        _log.info("  [INFO] 无PDF附件")
                        task_manager.increment_stats(task_id, skipped=1)
                        continue

                    site_type = 'hb' if 'hbba' in pattern else 'db'
                    _log.info(f"  [DOWN] 下载中（{'行业' if site_type == 'hb' else '地方'}标准）...")
                    pdf_data = await asyncio.get_running_loop().run_in_executor(
                        None, download_hb_with_captcha, hb_hash, site_type
                    )
                    if pdf_data:
                        filepath = get_output_dir() / filename
                        atomic_write(str(filepath), pdf_data, dir_=str(get_output_dir()))
                        _log.info(f"  [OK] {filepath} ({len(pdf_data)/1024:.0f}KB)")
                        task_manager.increment_stats(task_id, success=1, downloaded=1)
                        add_to_existing_files_cache(filename)
                    else:
                        _log.info("  [FAIL] 验证码下载失败")
                        task_manager.increment_stats(task_id, failed=1)
                except Exception as e:
                    _log.error(f"  [ERROR] {e}")
                    task_manager.increment_stats(task_id, failed=1)
            else:
                try:
                    loop = asyncio.get_running_loop()
                    resp = await loop.run_in_executor(None, http_client.get, detail_url)
                    resp.raise_for_status()
                    m = re.search(r'newGbInfo\?hcno=([A-Fa-f0-9]+)', resp.text)
                    if not m:
                        _log.info("  [WARN] 未找到 hcno，跳过")
                        task_manager.increment_stats(task_id, skipped=1)
                        continue
                    hcno = m.group(1)
                except Exception as e:
                    _log.error(f"  [ERROR] 获取详情页失败: {e}")
                    task_manager.increment_stats(task_id, failed=1)
                    continue

                filepath = get_output_dir() / filename

                try:
                    detail_url2 = f"{OPENSTD}/gb/newGbInfo?hcno={hcno}"
                    resp2 = await asyncio.get_running_loop().run_in_executor(
                        None, lambda: http_client.get(detail_url2))
                    resp2.raise_for_status()
                    html = resp2.text
                    has_download = bool(_RE_XZ_BTN.search(html))
                    has_preview = bool(_RE_CK_BTN.search(html))
                    copyright = '涉及版权保护' in html or '不提供在线阅读' in html or ('ISO、IEC' in html and '版权保护' in html)
                    can_dl_btn = has_download and not copyright
                    can_preview = has_preview and not copyright

                    if can_dl_btn:
                        _log.info("  [DOWN] 下载中...")
                        pdf_data = await asyncio.get_running_loop().run_in_executor(
                            None, download_with_captcha, hcno)
                        if pdf_data:
                            atomic_write(str(filepath), pdf_data, dir_=str(get_output_dir()))
                            _log.info(f"  [OK] {filepath} ({len(pdf_data)/1024:.0f}KB)")
                            task_manager.increment_stats(task_id, success=1, downloaded=1)
                            add_to_existing_files_cache(filename)
                        else:
                            _log.info("  [FAIL] 验证码下载失败")
                            task_manager.increment_stats(task_id, failed=1)

                    elif can_preview:
                        from app.scanner.preview import PLAYWRIGHT_AVAILABLE
                        from config.manager import load_config
                        if not (load_config().get('download', {}).get('allow_preview', True) and PLAYWRIGHT_AVAILABLE):
                            _log.info("  [SKIP] 预览拼接已禁用")
                            task_manager.increment_stats(task_id, skipped=1)
                        else:
                            _log.info("  [PREV] 预览中...")
                            if not browser_ctx:
                                playwright_mgr, browser_ctx = await launch_browser()
                            success = await preview_to_pdf(hcno, str(filepath), browser_ctx)
                            if success and filepath.stat().st_size > 1000:
                                _log.info(f"  [OK] {filepath} ({filepath.stat().st_size/1024:.0f}KB)")
                                task_manager.increment_stats(task_id, success=1, downloaded=1)
                                add_to_existing_files_cache(filename)
                            else:
                                _log.info("  [FAIL] 预览失败")
                                task_manager.increment_stats(task_id, failed=1)
                    else:
                        _log.info("  [NOBTN] 无下载/预览按钮" + ("(版权受限)" if copyright else ""))
                        task_manager.increment_stats(task_id, skipped=1)

                except Exception as e:
                    _log.error(f"  [ERROR] {e}")
                    task_manager.increment_stats(task_id, failed=1)

            await asyncio.sleep(DELAY)
    finally:
        if playwright_mgr:
            try:
                await playwright_mgr.__aexit__(None, None, None)
            except Exception as e:
                _log.debug(f"Playwright 关闭异常: {e}")


# 定时任务管理 API