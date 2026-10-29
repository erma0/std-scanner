"""scanner.search_download — 搜索结果单条下载

按 tid 分发 GB / 行业 / 地方 / 团体标准的下载流程，并把结果回写到
item 的 dlStatus / failReason 字段。供以下场景复用：
  - routes.search 批量搜索后的自动下载
  - 任务详情中的单条下载/重试
  - 搜索结果任务的一键下载全部
"""

import asyncio
import logging
from pathlib import Path

_log = logging.getLogger('std_scraper')

# 终态：这些状态不会因为重复下载而改变，一键下载时直接跳过
FINAL_STATUSES = {
    'downloaded', 'previewed', 'skipped_existing',
    'copyright', 'preview_disabled', 'no_fulltext', 'failed_no_pk',
}


def _resolve_site_pk(code, site_type):
    """按标准号回查 hbba/dbba 标准库，返回可下载的 pk（同步，供 executor 调用）。

    检索结果里的 pid 是 std.samr.gov.cn 的内部标识，与 hbba/dbba 的 pk 不同；
    详情页附件链接为空时必须用标准号回查真实 pk 才能下载。
    """
    if not code:
        return None
    from app.helpers import normalize_code
    try:
        if site_type == 'hb':
            from app.scanner.hb_scan import fetch_hb_list
            records = fetch_hb_list(industry='', key=code, status='', page=1, size=20).get('records', [])
        else:
            from app.scanner.db_scan import fetch_db_list
            records = fetch_db_list(province='', key=code, status='', page=1, size=20).get('records', [])
    except Exception as e:
        _log.warning(f"  [WARN] 回查 {site_type} 标准库失败: {code} {e}")
        return None

    target_code = normalize_code(code)
    for r in records:
        if normalize_code(r.get('code', '')) == target_code:
            return r.get('pk')
    _log.warning(f"  [WARN] {site_type} 标准库未匹配编号: {code}（返回 {len(records)} 条）")
    return None


class SearchDownloadSession:
    """搜索结果下载会话：复用一个去重缓存与浏览器上下文。

    浏览器仅在 GB 标准需要预览拼接时才按需启动。
    """

    def __init__(self, output_dir=None, existing=None):
        if existing is None:
            from app.dedup import get_existing_files
            existing = get_existing_files()
        self.existing = existing
        self.output_dir = Path(output_dir) if output_dir else None
        self._playwright = None
        self._browser_ctx = None

    async def close(self):
        if self._playwright:
            try:
                await self._playwright.__aexit__(None, None, None)
            except Exception as e:
                _log.debug(f"Playwright 关闭异常: {e}")
        self._playwright = None
        self._browser_ctx = None

    async def _ensure_browser(self):
        """按配置按需启动浏览器，返回 context 或 None（不可预览）。"""
        from app.scanner.preview import PLAYWRIGHT_AVAILABLE, launch_browser
        from config.manager import load_config
        if not PLAYWRIGHT_AVAILABLE:
            return None
        if not load_config().get('download', {}).get('allow_preview', True):
            return None
        if not self._browser_ctx:
            self._playwright, self._browser_ctx = await launch_browser()
        return self._browser_ctx

    @staticmethod
    def _mark(item, status, reason=None):
        item['dlStatus'] = status
        if reason:
            item['failReason'] = reason
        else:
            item.pop('failReason', None)
        return status in ('downloaded', 'previewed', 'skipped_existing'), status, reason or ''

    async def download_one(self, item, output_dir=None):
        """下载单条搜索结果，返回 (ok, status, reason)。

        item 需包含 code/name/tid/pid；执行后会写入 dlStatus / failReason。
        """
        from app.scanner.utils import make_filename
        from app.scanner.download_helpers import (
            fetch_and_save_pdf, extract_hcno_from_html, detect_download_buttons,
        )
        from app.scanner.search import get_detail_url_by_tid, check_hb_downloadable
        from app.scanner.hb_scan import download_hb_with_captcha, CopyrightError
        from app.scanner.download import download_with_captcha
        from app.scanner.preview import preview_to_pdf
        from config.settings import http_client, OPENSTD, get_output_dir

        out_dir = Path(output_dir) if output_dir else (self.output_dir or get_output_dir())
        out_dir.mkdir(parents=True, exist_ok=True)
        tid = item.get('tid', '')
        pid = item.get('pid', '')
        code = item.get('code') or item.get('stdCode', '')
        name = item.get('name') or item.get('stdName', '')
        loop = asyncio.get_running_loop()

        filename = make_filename(code, name)
        filepath = out_dir / filename

        if filename.lower() in self.existing:
            _log.info(f"  [SKIP] 已存在: {filename}")
            return self._mark(item, 'skipped_existing')

        # === 团体标准：httpx 直连 API 下载 ===
        if tid == 'BV_TT':
            from app.scanner.tt_scan import _download_one_standard
            std = {'detail_id': pid, 'pid': pid, 'code': code, 'name': name, 'sellable': False}
            try:
                await _download_one_standard(std, self.existing)
            except Exception as e:
                _log.error(f"  [TT ERROR] {code} {e}")
                return self._mark(item, 'failed', f'{type(e).__name__}: {e}')
            status = std.get('dlStatus') or 'failed'
            item['dlStatus'] = status
            if status == 'downloaded':
                item.pop('failReason', None)
                self.existing.add(filename.lower())
                return True, status, ''
            reason = std.get('failReason') or '下载失败'
            item['failReason'] = reason
            return False, status, reason

        # === 行业/地方标准：优先取检索详情页附件 hash，缺失时回查 hbba/dbba API ===
        if tid in ('BV_HB', 'BV_DB'):
            detail_url, _ = get_detail_url_by_tid(tid, pid)
            hb_hash, site_type = None, ('hb' if tid == 'BV_HB' else 'db')
            try:
                can_download, found_hash, pattern = await loop.run_in_executor(
                    None, check_hb_downloadable, detail_url)
                if can_download and found_hash:
                    hb_hash = found_hash
                    site_type = 'hb' if 'hbba' in pattern else 'db'
            except Exception as e:
                _log.debug(f"  [INFO] 检索详情页无附件链接: {code} {e}")

            if not hb_hash:
                # 检索详情页的附件链接经常为空，回查标准库 API 拿真实 pk
                hb_hash = await loop.run_in_executor(
                    None, _resolve_site_pk, code, site_type)
            if not hb_hash:
                _log.info(f"  [INFO] 无PDF附件: {code}")
                return self._mark(item, 'no_fulltext', '无PDF附件')
            _log.info(f"  [DOWN] {code} 下载中（{'行业' if site_type == 'hb' else '地方'}标准）...")
            reason_out = {}
            try:
                pdf_data = await loop.run_in_executor(
                    None, fetch_and_save_pdf,
                    lambda: download_hb_with_captcha(hb_hash, site_type, reason_out=reason_out),
                    filepath, filename, out_dir)
            except CopyrightError as ce:
                _log.info(f"  [COPY] 版权限制: {ce}")
                return self._mark(item, 'copyright', f'版权限制: {ce}')
            except Exception as e:
                _log.error(f"  [ERROR] {code} {e}")
                return self._mark(item, 'failed', f'{type(e).__name__}: {e}')
            if pdf_data:
                _log.info(f"  [OK] {filepath} ({len(pdf_data)/1024:.0f}KB)")
                self.existing.add(filename.lower())
                return self._mark(item, 'downloaded')
            return self._mark(item, 'failed', reason_out.get('reason', '未知原因'))

        # === 国家标准：hcno + 验证码下载，失败时按需预览拼接 ===
        detail_url, _ = get_detail_url_by_tid(tid, pid)
        try:
            resp = await loop.run_in_executor(None, http_client.get, detail_url)
            resp.raise_for_status()
            hcno = extract_hcno_from_html(resp.text)
        except Exception as e:
            _log.error(f"  [ERROR] 获取详情页失败: {code} {e}")
            return self._mark(item, 'failed', f'{type(e).__name__}: {e}')
        if not hcno:
            _log.info(f"  [WARN] 未找到 hcno: {code}")
            return self._mark(item, 'failed_no_hcno', 'hcno 未分配（标准太新）')

        try:
            detail_url2 = f"{OPENSTD}/gb/newGbInfo?hcno={hcno}"
            resp2 = await loop.run_in_executor(None, http_client.get, detail_url2)
            resp2.raise_for_status()
            btns = detect_download_buttons(resp2.text)
        except Exception as e:
            _log.error(f"  [ERROR] {code} {e}")
            return self._mark(item, 'failed', f'{type(e).__name__}: {e}')

        if btns.can_download:
            _log.info(f"  [DOWN] {code} 下载中...")
            reason_out = {}
            try:
                pdf_data = await loop.run_in_executor(
                    None, fetch_and_save_pdf,
                    lambda: download_with_captcha(hcno, reason_out=reason_out),
                    filepath, filename, out_dir)
            except Exception as e:
                _log.error(f"  [ERROR] {code} {e}")
                return self._mark(item, 'failed', f'{type(e).__name__}: {e}')
            if pdf_data:
                _log.info(f"  [OK] {filepath} ({len(pdf_data)/1024:.0f}KB)")
                self.existing.add(filename.lower())
                return self._mark(item, 'downloaded')
            return self._mark(item, 'failed', reason_out.get('reason', '未知原因'))

        if btns.can_preview:
            browser_ctx = await self._ensure_browser()
            if not browser_ctx:
                _log.info(f"  [SKIP] 预览拼接不可用: {code}")
                return self._mark(item, 'preview_disabled', '预览拼接未启用')
            _log.info(f"  [PREV] {code} 预览中...")
            success = await preview_to_pdf(hcno, str(filepath), browser_ctx)
            if success and filepath.exists() and filepath.stat().st_size > 1000:
                _log.info(f"  [OK] {filepath} ({filepath.stat().st_size/1024:.0f}KB)")
                self.existing.add(filename.lower())
                return self._mark(item, 'previewed')
            return self._mark(item, 'failed_preview', '预览拼接失败')

        reason = '版权限制，不提供全文' if btns.copyright else '无下载/预览按钮'
        _log.info(f"  [NOBTN] {code} {reason}")
        return self._mark(item, 'copyright' if btns.copyright else 'no_fulltext', reason)


async def download_single_search_item(item, output_dir=None):
    """独立下载单条搜索结果（不复用浏览器），供接口单条重试使用。"""
    session = SearchDownloadSession(output_dir)
    try:
        return await session.download_one(item, output_dir)
    finally:
        await session.close()
