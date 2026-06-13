"""标准速递 — 扫描+下载核心模块 (v3.9.1)

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
  quick      - quick_download, quick_download_web, generate_report, main
  change_tracker - compare_snapshot
"""

from app.scanner.preview import launch_browser, preview_to_pdf, PLAYWRIGHT_AVAILABLE
from app.scanner.utils import make_filename, compute_download_stats
from app.scanner.checkpoint import (
    load_scan_checkpoint, get_incr_checkpoint, update_incr_checkpoint, reset_incr_checkpoint,
)
from app.scanner.progress import save_progress
from app.scanner.download import download_with_captcha
from app.scanner.hb_scan import (
    download_hb_with_captcha, fetch_hb_list, scan_hb_standards, download_hb_standards,
)
from app.scanner.gb_scan import fetch_api_page, scan_pages, extract_hcno, download_phase
from app.scanner.search import (
    fetch_stdpage_search, get_detail_url_by_tid,
    check_downloadable, check_hb_downloadable,
)
from app.scanner.db_scan import fetch_db_list, scan_db_standards, download_db_standards
from app.scanner.quick import quick_download, quick_download_web, generate_report, main
from app.scanner.change_tracker import compare_snapshot

__all__ = [
    'launch_browser', 'preview_to_pdf', 'PLAYWRIGHT_AVAILABLE',
    'make_filename', 'compute_download_stats',
    'load_scan_checkpoint', 'get_incr_checkpoint', 'update_incr_checkpoint', 'reset_incr_checkpoint',
    'save_progress',
    'download_with_captcha', 'download_hb_with_captcha',
    'fetch_api_page', 'scan_pages', 'extract_hcno', 'download_phase',
    'fetch_stdpage_search', 'get_detail_url_by_tid', 'check_downloadable', 'check_hb_downloadable',
    'fetch_hb_list', 'scan_hb_standards', 'download_hb_standards',
    'fetch_db_list', 'scan_db_standards', 'download_db_standards',
    'quick_download', 'quick_download_web', 'generate_report', 'main',
    'compare_snapshot',
]
