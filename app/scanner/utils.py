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
    fail_reasons = {}
    for s in standards:
        st = s.get('dlStatus') or ''
        if not st:
            pending += 1
        elif st == 'downloading':
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
            reason = str(s.get('failReason') or st).strip()
            if reason:
                fail_reasons[reason] = fail_reasons.get(reason, 0) + 1
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
        'fail_reasons': fail_reasons,
    }


_DL_STATUS_LABELS = {
    'downloaded': '已下载',
    'previewed': '预览合成',
    'skipped_existing': '本地已存在',
    'skipped_obsolete': '已废止',
    'copyright': '版权保护',
    'preview_disabled': '预览未启用',
    'no_fulltext': '无全文',
    'no_hcno': '未发布',
    'downloading': '下载中',
    'failed': '下载失败',
    'failed_captcha': '验证码失败',
    'failed_preview': '预览失败',
    'failed_hcno': 'hcno 异常',
    'failed_no_hcno': '未发布',
    'failed_no_pk': '无标识',
}


def dl_status_label(status):
    """把内部 dlStatus 转成面向用户的中文文案。"""
    if not status:
        return '待下载'
    if status in _DL_STATUS_LABELS:
        return _DL_STATUS_LABELS[status]
    if status.startswith('failed_') or status.startswith('error:'):
        return '下载异常'
    return status


def _short_text(text, limit=40):
    """压缩多行/超长失败原因为单行短文本。"""
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def format_download_summary(stats, prefix='处理完成'):
    """按真实下载结果生成任务结束文案。

    只有真正落盘（success > 0）才使用"下载完成"式表述；全部不可下载/跳过时
    明确说明"未下载任何文件"，避免把版权保护误读成下载成功。
    有失败时附带出现次数最多的主要原因，便于直接判断问题类型。
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

    reason_tail = ''
    fail_reasons = stats.get('fail_reasons') or {}
    if failed and fail_reasons:
        top = sorted(fail_reasons.items(), key=lambda x: -x[1])[:3]
        reason_tail = '；主要原因: ' + '；'.join(
            f'{_short_text(r)}（{c}条）' for r, c in top)

    if not success and not failed and not pending:
        return '未下载任何文件：' + '，'.join(parts) + reason_tail
    return f'{prefix}：' + '，'.join(parts) + reason_tail


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


class ScanErrorCollector:
    """统一收集扫描页请求失败，生成日志、任务告警与失败文案。

    支持两种回调签名：
      - GB/TT: (page, exc)
      - HB/DB: (label, page, exc)

    用法：
        errors = ScanErrorCollector(task_manager, task_id, '[GB]')
        errors.reset()
        await scan_pages(..., on_error=errors.callback)
        message = '扫描完成' + errors.message_suffix()
        errors.publish()
        errors.raise_if_empty(standards)
    """

    def __init__(self, task_manager=None, task_id=None, log_prefix=''):
        self.task_manager = task_manager
        self.task_id = task_id
        self.log_prefix = log_prefix
        self.errors = []

    def reset(self):
        """开始新一轮扫描：清空计数并清除任务上的旧告警。"""
        self.errors = []
        if self.task_manager and self.task_id:
            self.task_manager.update(self.task_id, scan_note='', persist_std_items=False)

    @property
    def count(self):
        return len(self.errors)

    def callback(self, *args):
        if len(args) == 2:
            label, page, exc = '', args[0], args[1]
        else:
            label, page, exc = args
        prefix = f'{label} ' if label else ''
        msg = f'{prefix}第{page}页 {type(exc).__name__}: {exc}'
        self.errors.append(msg)
        _log.warning(f"{self.log_prefix} 扫描请求失败: {msg}")

    def note(self):
        """部分失败时的提示文案；无失败返回空字符串。"""
        if not self.errors:
            return ''
        return (f'{len(self.errors)} 页请求失败，结果可能不完整'
                f'（最近：{self.errors[-1]}）')

    def message_suffix(self):
        note = self.note()
        return f'，{note}' if note else ''

    def publish(self):
        """把告警写入任务字段（供前端卡片展示），返回告警文案。"""
        note = self.note()
        if self.task_manager and self.task_id:
            self.task_manager.update(self.task_id, scan_note=note, persist_std_items=False)
        return note

    def fail_message(self):
        """完全无结果时的失败文案；无失败返回空字符串。"""
        if not self.errors:
            return ''
        return f'扫描失败：{len(self.errors)} 页请求失败（最近：{self.errors[-1]}）'

    def raise_if_empty(self, standards):
        """扫描无结果且有请求失败时抛出明确异常，避免误报"无符合条件标准"。"""
        if not standards and self.errors:
            msg = self.fail_message()
            _log.error(f"{self.log_prefix} {msg}")
            raise RuntimeError(msg)
