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
    - 'skipped_obsolete'   → 已废止，默认跳过
    - 'copyright'          → 版权保护
    - 'preview_disabled'   → 有预览但用户关闭了预览功能
    - 'no_fulltext'        → 不可下载（无全文/未收录）
    - 'no_hcno'            → hcno 未分配（标准太新，未发布到 openstd）
    - None / 空字符串       → 待下载

    Returns:
        dict: {
            scanned,              # 总匹配数
            downloaded,           # 下载尝试总数 (direct_dl + previewed + failed)
            success,              # 成功总数 (direct_dl + previewed)
            failed,               # 失败总数
            skipped,              # 跳过总数 (已存在 + 废止 + 不可下载)
            direct_dl,            # 直接 PDF 下载数
            previewed,            # 预览拼接下载数
            skipped_existing,     # 文件已存在跳过数
            skipped_obsolete,     # 已废止跳过数
            skipped_nodl,         # 不可下载数（版权/无按钮）
            copyright,            # 其中版权保护数量
            pending,              # 尚未处理数（无 dlStatus）
        }
    """
    direct_dl = 0
    previewed = 0
    failed = 0
    skipped_existing = 0
    skipped_obsolete = 0
    skipped_nodl = 0
    copyright_restricted = 0
    pending = 0
    for s in standards:
        st = s.get('dlStatus') or ''
        if not st:
            pending += 1
        elif st == 'downloaded':
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
        elif st == 'skipped_obsolete':
            skipped_obsolete += 1
        elif st == 'copyright':
            copyright_restricted += 1
            skipped_nodl += 1
        elif st in ('preview_disabled', 'no_fulltext', 'no_hcno'):
            skipped_nodl += 1
    success = direct_dl + previewed
    downloaded = success + failed
    skipped = skipped_existing + skipped_obsolete + skipped_nodl
    return {
        'scanned': len(standards),
        'downloaded': downloaded,
        'success': success,
        'failed': failed,
        'skipped': skipped,
        'direct_dl': direct_dl,
        'previewed': previewed,
        'skipped_existing': skipped_existing,
        'skipped_obsolete': skipped_obsolete,
        'skipped_nodl': skipped_nodl,
        'copyright': copyright_restricted,
        'pending': pending,
    }


def format_download_summary(stats, prefix='处理完成'):
    """按真实下载结果生成任务结束文案。

    只有真正落盘（success > 0）才使用"下载完成"式表述；全部不可下载/跳过时
    明确说明"未下载任何文件"，避免把版权保护误读成下载成功。
    """
    success = stats.get('success', 0)
    failed = stats.get('failed', 0)
    existing = stats.get('skipped_existing', 0)
    obsolete = stats.get('skipped_obsolete', 0)
    nodl = stats.get('skipped_nodl', 0)
    copyright_restricted = stats.get('copyright', 0)
    pending = stats.get('pending', 0)

    parts = []
    if success:
        parts.append(f'成功 {success}')
    if failed:
        parts.append(f'失败 {failed}')
    if existing:
        parts.append(f'已存在 {existing}')
    if nodl:
        parts.append(f'不可下载 {nodl}' + (f'（版权保护 {copyright_restricted}）' if copyright_restricted else ''))
    if obsolete:
        parts.append(f'废止跳过 {obsolete}')
    if pending:
        parts.append(f'待下载 {pending}')

    if not parts:
        return '没有需要处理的标准'
    if not success and not failed and not pending:
        return '未下载任何文件：' + '，'.join(parts)
    return f'{prefix}：' + '，'.join(parts)


def make_filename(code, name, suffix='.pdf'):
    """生成文件名: code + name + suffix

    内部使用 safe_filename 清理非法字符，并限制总长度以适应
    Windows 260 字符路径限制。
    """
    c = code.strip()
    n = name.strip()

    max_code_name = 160
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
