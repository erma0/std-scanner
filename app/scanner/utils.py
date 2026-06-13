"""scanner.utils — 文件名生成与工具"""

import logging

from app.helpers import safe_filename

_log = logging.getLogger('std_scraper')


def compute_download_stats(standards):
    """从 standards 列表的 dlStatus 计算统一统计值。

    dlStatus 值约定（跨 GB/HB/DB 统一）：
    - 'downloaded'         → 直接下载成功
    - 'previewed'          → 预览拼接下载
    - 'failed' / 'failed_*'→ 下载失败
    - 'skipped_existing'   → 文件已存在跳过
    - 'copyright'          → 版权保护
    - 'preview_disabled'   → 有预览但用户关闭了预览功能
    - 'no_fulltext'        → 不可下载（无全文/未收录）
    - 'no_hcno'            → hcno 未分配（标准太新，未发布到 openstd）
    - None / 空字符串       → 不计入下载统计

    Returns:
        dict: {
            scanned,              # 总匹配数
            downloaded,           # 下载尝试总数 (direct_dl + previewed + failed)
            success,              # 成功总数 (direct_dl + previewed)
            failed,               # 失败总数
            skipped,              # 跳过总数 (skipped_existing + skipped_nodl)
            direct_dl,            # 直接 PDF 下载数
            previewed,            # 预览拼接下载数
            skipped_existing,     # 文件已存在跳过数
            skipped_nodl,         # 不可下载数（版权/无按钮）
        }
    """
    direct_dl = 0
    previewed = 0
    failed = 0
    skipped_existing = 0
    skipped_nodl = 0
    for s in standards:
        st = s.get('dlStatus') or ''
        if st == 'downloaded':
            direct_dl += 1
        elif st == 'previewed':
            previewed += 1
        elif st == 'failed' or st.startswith('failed_') or st.startswith('error:'):
            # failed_no_hcno / failed_no_pk 属于"不可下载"而非"下载失败"
            if st in ('failed_no_hcno', 'failed_no_pk'):
                skipped_nodl += 1
                continue
            failed += 1
        elif st == 'skipped_existing':
            skipped_existing += 1
        elif st in ('copyright', 'preview_disabled', 'no_fulltext', 'no_hcno'):
            skipped_nodl += 1
    success = direct_dl + previewed
    downloaded = success + failed
    skipped = skipped_existing + skipped_nodl
    return {
        'scanned': len(standards),
        'downloaded': downloaded,
        'success': success,
        'failed': failed,
        'skipped': skipped,
        'direct_dl': direct_dl,
        'previewed': previewed,
        'skipped_existing': skipped_existing,
        'skipped_nodl': skipped_nodl,
    }


def make_filename(code, name):
    """生成文件名: code + name + .pdf

    内部使用 safe_filename 清理非法字符，并限制总长度以适应
    Windows 260 字符路径限制。
    """
    c = code.strip()
    n = name.strip()

    max_code_name = 160
    suffix = '.pdf'
    total = len(c) + len(n)
    if total > max_code_name:
        available = max_code_name - len(c) - 1
        if available < 20:
            available_for_code = max(20, max_code_name - 23)
            c = c[:available_for_code] + '_'
            available = max_code_name - len(c) - 1
        n = n[:available] + '_'
    raw = f"{c} {n}{suffix}"
    return safe_filename(raw)
