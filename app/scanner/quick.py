"""scanner.quick — CLI 快捷搜索下载入口"""

import asyncio
import json
import os
import sys
import time
import logging
import re
from pathlib import Path

from version import VERSION, APP_NAME
from config.paths import BASE_DIR, DATA_FILE
from config.settings import (
    OUTPUT_DIR, REPORT_FILE, DETAIL_URL, OPENSTD,
    DELAY,
    http_client, HB_CODE_MAP, HB_SAFETY_CODES,
)
from app.helpers import atomic_write
from app.keywords import load_keywords, clean_name
from app.dedup import get_existing_files, add_to_existing_files_cache

from app.scanner.gb_scan import scan_pages, extract_hcno, download_phase, fetch_api_page, _RE_XZ_BTN, _RE_CK_BTN
from app.scanner.hb_scan import scan_hb_standards, download_hb_standards, download_hb_with_captcha
from app.scanner.db_scan import scan_db_standards, download_db_standards
from app.scanner.download import download_with_captcha
from app.scanner.preview import launch_browser, preview_to_pdf
from app.scanner.search import fetch_stdpage_search, check_downloadable, check_hb_downloadable, get_detail_url_by_tid
from app.scanner.utils import make_filename
from app.scanner.progress import save_progress

_log = logging.getLogger('std_scraper')

_cli_delay = DELAY
_cli_output_dir = OUTPUT_DIR


async def quick_download(query):
    """搜索指定标准号/名称并一键下载"""
    _log.info(f"[SEARCH] 搜索: {query}")
    try:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, fetch_api_page, 1, query)
    except Exception as e:
        _log.error(f"[ERROR] 搜索失败: {e}")
        return

    rows = data.get('rows', [])
    if not rows:
        _log.info("[NOTFOUND] 无匹配结果")
        return

    row = rows[0]
    code = row.get('C_STD_CODE', '')
    name = clean_name(row.get('C_C_NAME', ''))
    std_id = row['id']
    _log.info(f"[FOUND] {code} {name}")

    try:
        resp = await asyncio.get_running_loop().run_in_executor(
            None, lambda: http_client.get(f"{DETAIL_URL}?id={std_id}"))
        resp.raise_for_status()
        m = re.search(r'newGbInfo\?hcno=([A-Fa-f0-9]+)', resp.text)
        if not m:
            _log.warning("[ERROR] 未找到 hcno")
            return
        hcno = m.group(1)
    except Exception as e:
        _log.error(f"[ERROR] hcno 提取失败: {e}")
        return

    out_dir = _cli_output_dir
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    filename = make_filename(code, name)
    filepath = out_dir / filename

    existing = get_existing_files()
    if filename in existing:
        _log.info(f"[SKIP] 已存在: {filepath}")
        return

    try:
        detail_url = f"{OPENSTD}/gb/newGbInfo?hcno={hcno}"
        resp = await asyncio.get_running_loop().run_in_executor(
            None, lambda: http_client.get(detail_url))
        resp.raise_for_status()
        html = resp.text
        has_download = bool(_RE_XZ_BTN.search(html))
        has_preview = bool(_RE_CK_BTN.search(html))
        copyright = '涉及版权保护' in html or '不提供在线阅读' in html or ('ISO、IEC' in html and '版权保护' in html)
        can_dl = has_download and not copyright
        can_preview = has_preview and not copyright

        if can_dl:
            _log.info("[DOWN] 下载中...")
            loop = asyncio.get_running_loop()
            pdf_data = await loop.run_in_executor(None, download_with_captcha, hcno)
            if pdf_data:
                atomic_write(str(filepath), pdf_data, dir_=str(out_dir))
                _log.info(f"[OK] {filepath} ({len(pdf_data)/1024:.0f}KB)")
                add_to_existing_files_cache(filename)
            else:
                _log.warning("[FAIL] 验证码下载失败")
        elif can_preview:
            from app.scanner.preview import PLAYWRIGHT_AVAILABLE
            from config.manager import load_config
            if not (load_config().get('download', {}).get('allow_preview', True) and PLAYWRIGHT_AVAILABLE):
                _log.info("[SKIP] 预览拼接已禁用")
            else:
                _log.info("[PREV] 预览中...")
                pw_mgr, browser_ctx = await launch_browser()
                try:
                    success = await preview_to_pdf(hcno, str(filepath), browser_ctx)
                    if success and filepath.stat().st_size > 1000:
                        _log.info(f"[OK] {filepath} ({filepath.stat().st_size/1024:.0f}KB)")
                        add_to_existing_files_cache(filename)
                    else:
                        _log.warning("[FAIL] 预览失败")
                finally:
                    if pw_mgr:
                        try:
                            await pw_mgr.__aexit__(None, None, None)
                        except Exception as e:
                            _log.debug(f"Playwright 关闭异常: {e}")
        else:
            _log.info("[NOBTN] 无下载/预览按钮" + ("(版权受限)" if copyright else ""))
    except Exception as e:
        _log.error(f"[ERROR] {e}")


async def quick_download_web(query, std_type='国家标准', max_results=5):
    """使用标准检索网站搜索并一键下载"""
    _log.info(f"[SEARCH-WEB] 搜索: {query} (类型: {std_type})")

    try:
        standards, total = fetch_stdpage_search(query, page=1, std_type=std_type)
    except Exception as e:
        _log.error(f"[ERROR] 搜索失败: {e}")
        return

    if not standards:
        _log.info(f"[NOTFOUND] 未找到匹配 '{query}' 的标准")
        return

    _log.info(f"[FOUND] 共找到 {total} 条结果，显示前 {min(len(standards), max_results)} 条:")

    for i, s in enumerate(standards[:max_results]):
        can_dl, dl_msg = check_downloadable(s['tid'])
        dl_indicator = '📥' if can_dl else '🔒'
        _log.info(f"  {i+1}. {s['code']} {s['name']} [{s['status']}] {dl_indicator}")

    to_download = standards[:max_results]

    if len(standards) > max_results:
        _log.info(f"  ... 还有 {total - max_results} 条结果，请使用 --type 指定类型缩小范围")

    _log.info("")

    existing = get_existing_files()
    out_dir = _cli_output_dir

    playwright_mgr = None
    browser_ctx = None

    for i, s in enumerate(to_download):
        code = s['code']
        name = s['name']
        pid = s['pid']
        tid = s['tid']

        _log.info(f"[{i+1}/{len(to_download)}] 处理: {code} {name}")

        filename = make_filename(code, name)
        if filename in existing:
            _log.info(f"   [SKIP] 已存在: {filename}")
            continue

        can_dl, dl_msg = await asyncio.get_running_loop().run_in_executor(
            None, check_downloadable, tid)

        if not can_dl:
            _log.info(f"   [INFO] {dl_msg}")
            continue

        detail_url, _ = await asyncio.get_running_loop().run_in_executor(
            None, get_detail_url_by_tid, tid, pid)

        if tid in ('BV_HB', 'BV_DB'):
            try:
                can_download, hb_hash, pattern = await asyncio.get_running_loop().run_in_executor(
                    None, check_hb_downloadable, detail_url)
                if not can_download or not hb_hash:
                    _log.info("   [INFO] 无PDF附件（标准未提供电子版）")
                    continue

                site_type = 'hb' if 'hbba' in pattern else 'db'
                std_name = '行业标准' if site_type == 'hb' else '地方标准'

                _log.info(f"   [DOWN] 下载中（{std_name}）...")
                pdf_data = await asyncio.get_running_loop().run_in_executor(
                    None, download_hb_with_captcha, hb_hash, site_type
                )
                if pdf_data:
                    filepath = out_dir / filename
                    atomic_write(str(filepath), pdf_data, dir_=str(out_dir))
                    _log.info(f"   [OK] {filepath} ({len(pdf_data)/1024:.0f}KB)")
                    add_to_existing_files_cache(filename)
                else:
                    _log.warning("   [FAIL] 验证码下载失败")
            except Exception as e:
                _log.error(f"   [ERROR] {e}")
        else:
            try:
                loop = asyncio.get_running_loop()
                resp = await loop.run_in_executor(None, lambda: http_client.get(detail_url))
                resp.raise_for_status()
                m = re.search(r'newGbInfo\?hcno=([A-Fa-f0-9]+)', resp.text)
                if not m:
                    _log.warning("   [WARN] 未找到 hcno，跳过")
                    continue
                hcno = m.group(1)
            except Exception as e:
                _log.error(f"   [ERROR] 获取详情页失败: {e}")
                continue

            filepath = out_dir / filename

            try:
                detail_url2 = f"{OPENSTD}/gb/newGbInfo?hcno={hcno}"
                resp2 = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: http_client.get(detail_url2))
                resp2.raise_for_status()
                html = resp2.text
                has_download = bool(_RE_XZ_BTN.search(html))
                has_preview = bool(_RE_CK_BTN.search(html))
                copyright = '涉及版权保护' in html or '不提供在线阅读' in html or ('ISO、IEC' in html and '版权保护' in html)
                can_dl = has_download and not copyright
                can_preview = has_preview and not copyright

                if can_dl:
                    _log.info("   [DOWN] 下载中...")
                    pdf_data = await asyncio.get_running_loop().run_in_executor(
                        None, download_with_captcha, hcno)
                    if pdf_data:
                        atomic_write(str(filepath), pdf_data, dir_=str(out_dir))
                        _log.info(f"   [OK] {filepath} ({len(pdf_data)/1024:.0f}KB)")
                        add_to_existing_files_cache(filename)
                    else:
                        _log.warning("   [FAIL] 验证码下载失败")

                elif can_preview:
                    from app.scanner.preview import PLAYWRIGHT_AVAILABLE
                    from config.manager import load_config
                    if not (load_config().get('download', {}).get('allow_preview', True) and PLAYWRIGHT_AVAILABLE):
                        _log.info("   [SKIP] 预览拼接已禁用")
                    else:
                        _log.info("   [PREV] 预览中...")
                        if not browser_ctx:
                            playwright_mgr, browser_ctx = await launch_browser()
                        success = await preview_to_pdf(hcno, str(filepath), browser_ctx)
                        if success and filepath.stat().st_size > 1000:
                            _log.info(f"   [OK] {filepath} ({filepath.stat().st_size/1024:.0f}KB)")
                            add_to_existing_files_cache(filename)
                        else:
                            _log.warning("   [FAIL] 预览失败")

                else:
                    _log.info("   [NOBTN] 无下载/预览按钮" + ("(版权受限)" if copyright else ""))

            except Exception as e:
                _log.error(f"   [ERROR] {e}")

        await asyncio.sleep(_cli_delay)

    if playwright_mgr:
        try:
            await playwright_mgr.__aexit__(None, None, None)
        except Exception as e:
            _log.debug(f"Playwright 关闭异常: {e}")

    _log.info(f"\n[DONE] 已处理 {len(to_download)} 条标准")


def generate_report(standards):
    icons = {
        'downloaded': '[OK]', 'skipped_existing': '[SKIP]',
        'previewed': '[PREV]', 'failed': '[FAIL]',
        'preview_disabled': '[NOPREV]', 'no_fulltext': '[NOBTN]', 'failed_preview': '[FAIL]',
        'copyright': '[COPY]', 'no_hcno': '[NOHCNO]',
    }
    def get_icon(s):
        st = s.get('dlStatus', '') or ''
        if st in icons:
            return icons[st]
        if st.startswith('error') or st.startswith('failed'):
            return '[FAIL]'
        return '[?]'
    lines = [
        '# 生产安全相关国家标准',
        '',
        f'> 生成: {time.strftime("%Y-%m-%d %H:%M:%S")}',
        f'> 共 {len(standards)} 条',
        '',
        '| # | 标准号 | 中文名称 | 类型 | 状态 | 发布 | 实施 | 下载 |',
        '|---|--------|---------|------|------|------|------|------|',
    ]
    for i, s in enumerate(standards):
        lines.append(f"| {i + 1} | {s['stdCode']} | {s['stdName']} | {s['stdNature']} | {s['state']} | {s['issueDate']} | {s['actDate']} | {get_icon(s)} |")
    lines.append('')
    lines.append(f'> 共 {len(standards)} 条')
    atomic_write(str(REPORT_FILE), '\n'.join(lines), mode='w')


async def main():
    global _cli_delay, _cli_output_dir

    args = sys.argv[1:]

    if '--help' in args or '-h' in args:
        safety_codes = ', '.join(f"{c}({HB_CODE_MAP[c]})" for c in HB_SAFETY_CODES)
        print(f"""{APP_NAME} v{VERSION}

用法:
  python scan_all.py --pages=N             扫N条+提取hcno+下载（默认500条）
  python scan_all.py --pages=N --scan-only  只扫描+提取hcno
  python scan_all.py --dl-only              仅从已有JSON下载
  python scan_all.py --incr                 增量模式
  python scan_all.py --search=关键词         搜索并一键下载（内部API）
  python scan_all.py --search-web=关键词     搜索并一键下载（标准检索网站）
  python scan_all.py --scan-hb[=行业]        扫描行业标准（默认安全相关行业）
  python scan_all.py --scan-db[=省份]        扫描地方标准（默认江苏省）
  python scan_all.py --help                 帮助

参数:
  --delay=3.0           所有请求间隔（默认3）
  --output-dir=PATH     PDF 存放路径（默认 ~/Downloads/安全标准）
  --type=类型           标准类型: 国家标准/行业标准/地方标准/国家标准计划（默认国家标准）
  --scan-only           仅扫描不下载
  --max-pages=N         扫描最大条数（默认500）
  --incr                增量扫描（记录上次采集位置，下次只扫新增）

行业代码（--scan-hb 可用代码或汉字）:
  {safety_codes}
  完整代码列表: AQ BB CB CH CJ CY DA DB DL DY DZ EJ FZ GA GC GF GH GM GY
                HB HG HJ HS HY JB JC JG JR JS JT JY KA LB LD LS LY MH MR
                MT MZ NB NY QB QC QJ QX RB RF SB SC SF SH SJ SL SN SW SY
                TB TD TY WB WH WJ WM WS WW XB XF YB YC YD YJ YS YY YZ ZY

示例:
  python scan_all.py --pages=10 --delay=5
  python scan_all.py --scan-hb                  默认安全相关行业(AQ,KA,XF,GA,LD,YJ)
  python scan_all.py --scan-hb=AQ,XF            用行业代码指定
  python scan_all.py --scan-hb=安全生产,化工      用汉字名称指定
  python scan_all.py --scan-hb=all              扫描全部行业
  python scan_all.py --scan-db                  默认江苏省
  python scan_all.py --scan-db=浙江省            指定省份
  python scan_all.py --scan-db=浙江省,上海市      指定多个省份
  python scan_all.py --scan-db=all              扫描全部省份
  python scan_all.py --scan-hb --keywords=my_kw.txt
  python scan_all.py --search-web=消火栓 --type=国家标准
        """)
        return

    search_query = next((a.split('=', 1)[1] for a in args if a.startswith('--search=')), None)
    if search_query:
        await quick_download(search_query)
        return

    search_web_query = next((a.split('=', 1)[1] for a in args if a.startswith('--search-web=')), None)
    if search_web_query:
        std_type = next((a.split('=', 1)[1] for a in args if a.startswith('--type=')), '国家标准')
        max_results = int(next((a.split('=')[1] for a in args if a.startswith('--max=')), '5'))
        await quick_download_web(search_web_query, std_type=std_type, max_results=max_results)
        return

    scan_hb_arg = next((a for a in args if a.startswith('--scan-hb')), None)
    scan_db_arg = next((a for a in args if a.startswith('--scan-db')), None)
    keywords_path = next((a.split('=', 1)[1] for a in args if a.startswith('--keywords=')), None)

    _cli_delay = float(next((a.split('=')[1] for a in args if a.startswith('--delay=')), str(DELAY)))

    output_dir_override = next((a.split('=', 1)[1] for a in args if a.startswith('--output-dir=')), None)
    if output_dir_override:
        _cli_output_dir = Path(output_dir_override)
        os.makedirs(_cli_output_dir, exist_ok=True)

    scan_only = '--scan-only' in args
    max_results = int(next((a.split('=')[1] for a in args if a.startswith('--max-pages=')), '500'))

    if keywords_path:
        loaded = load_keywords(keywords_path)
        print(f"[KW] 加载关键词: {keywords_path} ({len(loaded)}个)")

    if scan_hb_arg:
        hb_val = scan_hb_arg.split('=', 1)[1] if '=' in scan_hb_arg else ''
        if hb_val.lower() == 'all':
            industries = list(HB_CODE_MAP.values())
        elif hb_val:
            industries = [x.strip() for x in hb_val.split(',') if x.strip()]
        else:
            industries = None
        print(f"[HB] 行业标准扫描 行业:{industries or '默认安全相关'} 最大采集:{max_results}条")
        stds = scan_hb_standards(industries=industries, max_results=max_results)
        if not scan_only and stds:
            await download_hb_standards(stds)
        hb_data_file = BASE_DIR / "hb_standards.json"
        atomic_write(str(hb_data_file), json.dumps({'generatedAt': time.strftime('%Y-%m-%d %H:%M:%S'),
                           'total': len(stds), 'standards': stds}, ensure_ascii=False, indent=2), mode='w')
        print(f"[DONE] 行业标准: {len(stds)}条 数据:{hb_data_file}")
        return

    if scan_db_arg:
        db_val = scan_db_arg.split('=', 1)[1] if '=' in scan_db_arg else ''
        if db_val.lower() == 'all':
            provinces = ['']
        elif db_val:
            provinces = [x.strip() for x in db_val.split(',') if x.strip()]
        else:
            provinces = None
        print(f"[DB] 地方标准扫描 省份:{provinces or '默认江苏省'} 最大采集:{max_results}条")
        stds = scan_db_standards(provinces=provinces, max_results=max_results)
        if not scan_only and stds:
            await download_db_standards(stds)
        db_data_file = BASE_DIR / "db_standards.json"
        atomic_write(str(db_data_file), json.dumps({'generatedAt': time.strftime('%Y-%m-%d %H:%M:%S'),
                           'total': len(stds), 'standards': stds}, ensure_ascii=False, indent=2), mode='w')
        print(f"[DONE] 地方标准: {len(stds)}条 数据:{db_data_file}")
        return

    dl_only = '--dl-only' in args
    incr = '--incr' in args
    max_results = int(next((a.split('=')[1] for a in args if a.startswith('--pages=')), '500'))

    if dl_only:
        if not DATA_FILE.is_file():
            print("[ERROR] 无 safety_full.json, 请先扫描")
            return
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        stds = data['standards']
        print(f"[DL-ONLY] {len(stds)} 条")

        await extract_hcno(stds)
        save_progress(stds)

        await download_phase(stds)

        save_progress(stds)
        generate_report(stds)
        print(f"[DONE] {REPORT_FILE}")
        return

    print(f"[START] v{VERSION} 采集:{max_results}条 scan_only:{scan_only} incr:{incr} delay={_cli_delay}s output={_cli_output_dir}")

    stds = await scan_pages(max_results, incr)
    existing = get_existing_files()
    for s in stds:
        s['filename'] = make_filename(s['stdCode'], s['stdName'])
    print(f"\n[SCAN] {len(stds)} 条安全标准 (已有文件: {len(existing)})")

    if not scan_only:
        await extract_hcno(stds)
    save_progress(stds)

    if not scan_only:
        await download_phase(stds, existing)

    save_progress(stds)
    generate_report(stds)
    print(f"\n[DONE] JSON: {DATA_FILE} | Report: {REPORT_FILE} | PDF: {_cli_output_dir}")


if __name__ == '__main__':
    asyncio.run(main())
