"""API routes — Search"""
from fastapi import APIRouter, HTTPException, Request
import time
import asyncio
import logging

from ._utils import launch_task as _launch_task, update_task_status
from .state import task_manager

# 以下模块均为搜索/下载时才需要的重模块（scanner / captcha / dedup 链），
# 统一延迟到首次调用时导入，避免阻塞 API 启动。
# 涉及：app.scanner.*, app.dedup, config.settings (http_client/OPENSTD/DETAIL_URL)

_log = logging.getLogger('std_scraper')


router = APIRouter(prefix="", tags=["Search"])

@router.post("/api/search/query")
async def search_query_api(request: Request):
    from app.scanner import (
        fetch_stdpage_search, check_downloadable,
        search_tt_standards,
    )
    from config.settings import DETAIL_URL

    body = await request.json()
    query = body.get("query", "")
    std_type = body.get("std_type", "全部")
    max_results = body.get("max_results", 20)

    if not query:
        raise HTTPException(status_code=400, detail="搜索关键词不能为空")

    # 团体标准走独立 API 搜索（ttbz.org.cn，无 WAF，httpx 直接调用）
    if std_type == '团体标准':
        try:
            standards = await search_tt_standards(query, max_results)
        except Exception as e:
            _log.error(f"团体标准搜索失败: {e}")
            raise HTTPException(status_code=500, detail=f"团体标准搜索失败: {e}")
        # TT 结果已含 detail_id，downloadable 判断：非付费即可下载
        results = [{
            "code": s.get('code', ''),
            "name": s.get('name', ''),
            "tid": 'BV_TT',
            "pid": s.get('detail_id', ''),
            "status": s.get('status', ''),
            "publishDate": s.get('publishDate', ''),
            "actDate": '',
            "downloadable": not s.get('sellable', False),
            "download_msg": '付费标准' if s.get('sellable') else '可下载',
            "detailUrl": f"https://www.ttbz.org.cn/StandardManage/Detail/{s.get('detail_id', '')}/",
            "group_name": s.get('group_name', ''),
        } for s in standards]
        return {"success": True, "total": len(results), "results": results}

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


@router.post("/api/search/batch")
async def search_batch_api(request: Request):
    """批量搜索下载：每行一个关键词/标准号，逐个搜索去重后自动下载。

    body: {
        queries: ["关键词1", "标准号1", ...],   # 必填
        std_type: "全部" | "国家标准" | ...,    # 默认 "全部"
        per_query_max: 20,                      # 单个关键词最大结果数，默认 20
        auto_download: true                     # 搜索完成后自动下载，默认 true
    }

    返回 task_id，通过 SSE 推送进度：
      0-50%   搜索阶段（"搜索中 N/M: 关键词"）
      50-100% 下载阶段（复用 _download_selected_items）
    """
    body = await request.json()
    queries = body.get("queries", [])
    std_type = body.get("std_type", "全部")
    per_query_max = int(body.get("per_query_max", 20))
    auto_download = bool(body.get("auto_download", True))

    # 规范化 queries：字符串可按行拆分
    flat = []
    for q in queries:
        if isinstance(q, str):
            for line in q.splitlines():
                line = line.strip()
                if line and not line.startswith('#'):
                    flat.append(line)
    queries = flat

    if not queries:
        raise HTTPException(status_code=400, detail="未提供搜索关键词")
    if per_query_max < 1 or per_query_max > 100:
        raise HTTPException(status_code=400, detail="per_query_max 取值范围 1-100")

    task_id = f"search_batch_{int(time.time())}"
    task_manager.create(task_id, {
        "task_id": task_id,
        "status": "running",
        "progress": 0,
        "message": f"批量搜索 {len(queries)} 个关键词",
        "stats": {"downloaded": 0, "success": 0, "failed": 0, "skipped": 0, "searched": 0},
        "start_time": time.time(),
        "std_type": "search_batch",
        "std_items": [],
    })

    async def batch_task():
        try:
            _log.info(f"任务 {task_id}: 批量搜索 {len(queries)} 个关键词")
            all_items = await _batch_search_dedup(task_id, queries, std_type, per_query_max)

            if not all_items:
                update_task_status(task_id, progress=100, status="completed",
                                 message="搜索完成，未找到可下载的标准",
                                 end_time=time.time())
                return

            _log.info(f"任务 {task_id}: 去重后 {len(all_items)} 个标准，auto_download={auto_download}")
            if auto_download:
                update_task_status(task_id, progress=50,
                                 message=f"搜索完成（{len(all_items)} 个），开始下载...")
                await _download_selected_items(task_id, all_items, progress_base=50)
                update_task_status(task_id, progress=100, status="completed",
                                 message=f"批量任务完成（{len(all_items)} 个标准）",
                                 end_time=time.time())
            else:
                # 仅搜索：把结果存入 std_items 供前端查看
                task_manager.update(task_id, std_items=all_items, progress=100,
                                  status="completed",
                                  message=f"搜索完成，共 {len(all_items)} 个可下载标准",
                                  end_time=time.time())
            _log.info(f"任务 {task_id}: 完成")
        except Exception as e:
            _log.error(f"任务 {task_id} 异常: {e}", exc_info=True)
            task_manager.update(task_id, status="failed", message=str(e), end_time=time.time())

    _launch_task(batch_task(), f"batch-{task_id}")
    return {"success": True, "task_id": task_id, "queries_count": len(queries)}


async def _batch_search_dedup(task_id, queries, std_type, per_query_max):
    """批量搜索并去重，返回可下载的 items 列表。

    去重 key：GB 用 pid；HB/DB 用 code+name（pid 可能跨类型相同）；
    TT（团体标准）用 detail_id。
    """
    from app.scanner import fetch_stdpage_search, check_downloadable, search_tt_standards
    from config.settings import DETAIL_URL, get_delay

    seen = set()
    all_items = []
    total = len(queries)
    loop = asyncio.get_running_loop()
    is_tt = (std_type == '团体标准')

    for i, q in enumerate(queries):
        update_task_status(task_id,
                        progress=int(50 * i / total) if total else 50,
                        message=f"搜索中 {i+1}/{total}: {q[:30]}")
        try:
            if is_tt:
                # 团体标准走 httpx 直连 API 搜索
                standards = await search_tt_standards(q, per_query_max)
                total_count = len(standards)
            else:
                standards, total_count = await loop.run_in_executor(
                    None, fetch_stdpage_search, q, 1, std_type)
        except Exception as e:
            _log.warning(f"搜索 '{q}' 失败: {e}")
            continue

        added = 0
        for s in standards[:per_query_max]:
            if is_tt:
                # 团体标准结果格式
                tid = 'BV_TT'
                pid = s.get('detail_id', '')
                code = s.get('code', '')
                name = s.get('name', '')
                detail_id = pid
                key = f"BV_TT|{detail_id}"
                if key in seen:
                    continue
                seen.add(key)
                # 付费标准跳过
                if s.get('sellable'):
                    continue
                all_items.append({
                    "code": code,
                    "name": name,
                    "tid": tid,
                    "pid": pid,
                    "status": s.get('status', ''),
                    "publishDate": s.get('publishDate', ''),
                    "actDate": '',
                    "downloadable": True,
                    "download_msg": '可下载',
                    "detailUrl": f"https://www.ttbz.org.cn/StandardManage/Detail/{detail_id}/",
                    "query": q,
                })
                added += 1
                continue

            tid = s.get('tid', '')
            pid = s.get('pid', '')
            code = s.get('code', '')
            name = s.get('name', '')
            # 去重 key：GB 用 pid（30 char hex）；HB/DB tid 是 BV_HB/BV_DB，pid 是 64 位 hash
            key = f"{tid}|{pid}" if pid else f"{tid}|{code}|{name}"
            if key in seen:
                continue
            seen.add(key)

            can_dl, dl_msg = await loop.run_in_executor(None, check_downloadable, tid)
            if not can_dl:
                continue
            all_items.append({
                "code": code,
                "name": name,
                "tid": tid,
                "pid": pid,
                "status": s.get('status', ''),
                "publishDate": s.get('publishDate', ''),
                "actDate": s.get('actDate', ''),
                "downloadable": can_dl,
                "download_msg": dl_msg,
                "detailUrl": f"{DETAIL_URL}?id={tid}",
                "query": q,  # 记录命中的关键词，便于审计
            })
            added += 1

        _log.info(f"  [{i+1}/{total}] '{q[:30]}' +{added} (累计 {len(all_items)})")
        task_manager.increment_stats(task_id, searched=1)
        # 保护搜索站点
        await asyncio.sleep(get_delay())

    return all_items


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
        "message": f"下载中 {len(items)} 个标准",
        "stats": {"downloaded": 0, "success": 0, "failed": 0, "skipped": 0},
        "start_time": time.time(),
        "std_type": "search",
        "std_items": []
    })

    async def download_task():
        try:
            _log.info(f"任务 {task_id}: 下载选中标准 {len(items)} 个")
            await _download_selected_items(task_id, items, progress_base=0)

            update_task_status(task_id, progress=100, status="completed",
                             message=f"下载完成({len(items)}个标准)")
            task_manager.update(task_id, end_time=time.time())
            _log.info(f"任务 {task_id}: 完成")
        except Exception as e:
            _log.error(f"任务 {task_id} 异常: {e}")
            task_manager.update(task_id, status="failed", message=str(e), end_time=time.time())

    _launch_task(download_task(), f"download-{task_id}")
    return {"success": True, "task_id": task_id}


async def _download_selected_items(task_id, items, progress_base=0):
    """下载搜索结果。progress_base 控制进度条起始百分比：
    - 0  用于普通 /api/search/download（0-99%）
    - 50 用于 /api/search/batch（50-99%，前半段是搜索）
    """
    from app.scanner import (
        download_with_captcha, download_hb_with_captcha, CopyrightError,
        check_downloadable, check_hb_downloadable, get_detail_url_by_tid,
        preview_to_pdf, launch_browser, make_filename,
    )
    from app.scanner.download_helpers import (
        detect_download_buttons, extract_hcno_from_html, fetch_and_save_pdf,
    )
    from app.dedup import get_existing_files, add_to_existing_files_cache
    from config.settings import OPENSTD, get_output_dir, get_delay, http_client

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
            # 下载阶段映射到 progress_base - 99% 区间
            span = 99 - progress_base
            dl_pct = progress_base + int(span * i / total) if total else 99
            update_task_status(task_id,
                            progress=dl_pct,
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

            if tid == 'BV_TT':
                # 团体标准下载（httpx 直接调用 API，无 WAF）
                try:
                    from app.scanner.tt_scan import _download_one_standard
                    # 构造 std dict 供 _download_one_standard 使用
                    std_dict = {'detail_id': pid, 'pid': pid, 'code': code, 'name': name, 'sellable': False}
                    await _download_one_standard(std_dict, get_existing_files())
                    if std_dict.get('dlStatus') == 'downloaded':
                        task_manager.increment_stats(task_id, success=1, downloaded=1)
                    elif std_dict.get('dlStatus') == 'skipped_existing':
                        task_manager.increment_stats(task_id, skipped=1)
                    else:
                        task_manager.increment_stats(task_id, failed=1)
                        await asyncio.sleep(get_delay())
                except Exception as e:
                    _log.error(f"  [TT ERROR] {e}")
                    task_manager.increment_stats(task_id, failed=1)
                    await asyncio.sleep(get_delay())
            elif tid in ('BV_HB', 'BV_DB'):
                try:
                    can_download, hb_hash, pattern = await asyncio.get_running_loop().run_in_executor(
                        None, check_hb_downloadable, detail_url)
                    if not can_download or not hb_hash:
                        _log.info("  [INFO] 无PDF附件")
                        task_manager.increment_stats(task_id, skipped=1)
                        continue

                    site_type = 'hb' if 'hbba' in pattern else 'db'
                    _log.info(f"  [DOWN] 下载中（{'行业' if site_type == 'hb' else '地方'}标准）...")
                    out_dir = get_output_dir()
                    filepath = out_dir / filename
                    try:
                        pdf_data = await asyncio.get_running_loop().run_in_executor(
                            None, fetch_and_save_pdf,
                            lambda: download_hb_with_captcha(hb_hash, site_type),
                            filepath, filename, out_dir,
                        )
                    except CopyrightError as ce:
                        _log.info(f"  [COPY] 版权限制: {ce}")
                        task_manager.increment_stats(task_id, skipped=1)
                        continue
                    if pdf_data:
                        _log.info(f"  [OK] {filepath} ({len(pdf_data)/1024:.0f}KB)")
                        task_manager.increment_stats(task_id, success=1, downloaded=1)
                    else:
                        _log.info("  [FAIL] 下载失败")
                        task_manager.increment_stats(task_id, failed=1)
                        await asyncio.sleep(get_delay())
                except Exception as e:
                    _log.error(f"  [ERROR] {e}")
                    task_manager.increment_stats(task_id, failed=1)
                    await asyncio.sleep(get_delay())
            else:
                try:
                    loop = asyncio.get_running_loop()
                    resp = await loop.run_in_executor(None, http_client.get, detail_url)
                    resp.raise_for_status()
                    hcno = extract_hcno_from_html(resp.text)
                    if not hcno:
                        _log.info("  [WARN] 未找到 hcno，跳过")
                        task_manager.increment_stats(task_id, skipped=1)
                        continue
                except Exception as e:
                    _log.error(f"  [ERROR] 获取详情页失败: {e}")
                    task_manager.increment_stats(task_id, failed=1)
                    continue

                out_dir = get_output_dir()
                filepath = out_dir / filename

                try:
                    detail_url2 = f"{OPENSTD}/gb/newGbInfo?hcno={hcno}"
                    resp2 = await asyncio.get_running_loop().run_in_executor(
                        None, lambda: http_client.get(detail_url2))
                    resp2.raise_for_status()
                    btns = detect_download_buttons(resp2.text)

                    if btns.can_download:
                        _log.info("  [DOWN] 下载中...")
                        pdf_data = await asyncio.get_running_loop().run_in_executor(
                            None, fetch_and_save_pdf,
                            lambda: download_with_captcha(hcno),
                            filepath, filename, out_dir,
                        )
                        if pdf_data:
                            _log.info(f"  [OK] {filepath} ({len(pdf_data)/1024:.0f}KB)")
                            task_manager.increment_stats(task_id, success=1, downloaded=1)
                        else:
                            _log.info("  [FAIL] 下载失败")
                            task_manager.increment_stats(task_id, failed=1)
                            await asyncio.sleep(get_delay())

                    elif btns.can_preview:
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
                                await asyncio.sleep(get_delay())
                    else:
                        _log.info("  [NOBTN] 无下载/预览按钮" + ("(版权受限)" if btns.copyright else ""))
                        task_manager.increment_stats(task_id, skipped=1)

                except Exception as e:
                    _log.error(f"  [ERROR] {e}")
                    task_manager.increment_stats(task_id, failed=1)
                    await asyncio.sleep(get_delay())
    finally:
        if playwright_mgr:
            try:
                await playwright_mgr.__aexit__(None, None, None)
            except Exception as e:
                _log.debug(f"Playwright 关闭异常: {e}")


# 定时任务管理 API