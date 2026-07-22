"""标准速递 — 扫描+下载核心模块 (v1.0.0)

子模块:
  preview    - launch_browser, preview_to_pdf
  utils      - make_filename, compute_download_stats
  checkpoint - load_scan_checkpoint, get_incr_checkpoint, update_incr_checkpoint, reset_incr_checkpoint
  progress   - save_progress
  download   - download_with_captcha
  gb_scan    - fetch_api_page, scan_pages, extract_hcno, download_phase
  search     - fetch_stdpage_search, get_detail_url_by_tid, check_downloadable, check_hb_downloadable
  hb_scan    - fetch_hb_list, scan_hb_standards, download_hb_standards, download_hb_with_captcha
  db_scan    - fetch_db_list, scan_db_standards, download_db_standards
  tt_scan    - scan_tt_standards, download_tt_standards, run_tt_pipeline (团体标准, httpx 直连 cms-proxy API)
  mem_scan   - scan_mem_standards, download_mem_standards, run_mem_pipeline (应急管理部标准, httpx 直连 HTML)
  quick      - quick_download, quick_download_web, generate_report, main
  change_tracker - compare_snapshot
"""

# 延迟导出：避免模块导入时加载所有子模块（约 0.5s），
# 仅在首次访问具体名称时才导入对应的子模块。
_SUBMODULE_MAP = {
    'launch_browser': 'app.scanner.preview',
    'preview_to_pdf': 'app.scanner.preview',
    'browser_session': 'app.scanner.preview',
    'PLAYWRIGHT_AVAILABLE': 'app.scanner.preview',
    'make_filename': 'app.scanner.utils',
    'compute_download_stats': 'app.scanner.utils',
    'load_scan_checkpoint': 'app.scanner.checkpoint',
    'get_incr_checkpoint': 'app.scanner.checkpoint',
    'update_incr_checkpoint': 'app.scanner.checkpoint',
    'reset_incr_checkpoint': 'app.scanner.checkpoint',
    'save_progress': 'app.scanner.progress',
    'download_with_captcha': 'app.scanner.download',
    'download_hb_with_captcha': 'app.scanner.hb_scan',
    'fetch_hb_list': 'app.scanner.hb_scan',
    'scan_hb_standards': 'app.scanner.hb_scan',
    'download_hb_standards': 'app.scanner.hb_scan',
    'CopyrightError': 'app.scanner.hb_scan',
    'fetch_api_page': 'app.scanner.gb_scan',
    'scan_pages': 'app.scanner.gb_scan',
    'extract_hcno': 'app.scanner.gb_scan',
    'download_phase': 'app.scanner.gb_scan',
    'fetch_stdpage_search': 'app.scanner.search',
    'get_detail_url_by_tid': 'app.scanner.search',
    'check_downloadable': 'app.scanner.search',
    'check_hb_downloadable': 'app.scanner.search',
    'fetch_db_list': 'app.scanner.db_scan',
    'scan_db_standards': 'app.scanner.db_scan',
    'download_db_standards': 'app.scanner.db_scan',
    'scan_tt_standards': 'app.scanner.tt_scan',
    'download_tt_standards': 'app.scanner.tt_scan',
    'run_tt_pipeline': 'app.scanner.tt_scan',
    'search_tt_standards': 'app.scanner.tt_scan',
    'scan_mem_standards': 'app.scanner.mem_scan',
    'download_mem_standards': 'app.scanner.mem_scan',
    'run_mem_pipeline': 'app.scanner.mem_scan',
    'quick_download': 'app.scanner.quick',
    'quick_download_web': 'app.scanner.quick',
    'generate_report': 'app.scanner.quick',
    'main': 'app.scanner.quick',
    'compare_snapshot': 'app.scanner.change_tracker',
}

_cache = {}


def __getattr__(name):
    if name in _cache:
        return _cache[name]
    if name in _SUBMODULE_MAP:
        import importlib
        mod = importlib.import_module(_SUBMODULE_MAP[name])
        val = getattr(mod, name)
        _cache[name] = val
        return val
    raise AttributeError(f"module 'app.scanner' has no attribute {name!r}")


__all__ = list(_SUBMODULE_MAP.keys())
