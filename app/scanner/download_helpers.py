"""scanner.download_helpers — 下载流程共享工具

抽取 gb_scan / routes.search / quick 三处重复的逻辑：
  - 按钮检测 + 版权判定
  - hcno 提取
  - 下载 + 落盘 + 去重缓存（单次 executor 调用，避免 async 中同步 I/O）
"""

import re
import logging
from typing import Callable, NamedTuple, Optional

from app.helpers import atomic_write

_log = logging.getLogger('std_scraper')

# 按钮检测正则：匹配 <button> 标签中的 xz_btn / ck_btn class（排除 JS 选择器引用）
RE_XZ_BTN = re.compile(r'<button[^>]*class="[^"]*xz_btn[^"]*"[^>]*>')
RE_CK_BTN = re.compile(r'<button[^>]*class="[^"]*ck_btn[^"]*"[^>]*>')

# 详情页 hcno 提取正则：'...newGbInfo?hcno=ABC123...'
RE_HCNO = re.compile(r'newGbInfo\?hcno=([A-Fa-f0-9]+)')


class DownloadButtons(NamedTuple):
    """详情页按钮检测结果"""
    has_download: bool       # 是否有下载按钮
    has_preview: bool        # 是否有预览按钮
    copyright: bool          # 是否受版权保护
    can_download: bool       # 可直接下载（有下载按钮 且 非版权）
    can_preview: bool        # 可预览（有预览按钮 且 非版权）


def detect_download_buttons(html: str) -> DownloadButtons:
    """检测详情页 HTML 中的下载/预览按钮及版权限制"""
    has_download = bool(RE_XZ_BTN.search(html))
    has_preview = bool(RE_CK_BTN.search(html))
    copyright = (
        '涉及版权保护' in html
        or '不提供在线阅读' in html
        or ('ISO、IEC' in html and '版权保护' in html)
    )
    return DownloadButtons(
        has_download=has_download,
        has_preview=has_preview,
        copyright=copyright,
        can_download=has_download and not copyright,
        can_preview=has_preview and not copyright,
    )


def extract_hcno_from_html(html: str) -> Optional[str]:
    """从详情页 HTML 中提取 hcno（32 位十六进制）"""
    m = RE_HCNO.search(html)
    return m.group(1) if m else None


def fetch_and_save_pdf(
    download_fn: Callable[[], Optional[bytes]],
    filepath,
    filename: str,
    output_dir,
) -> Optional[bytes]:
    """执行下载 + 原子落盘 + 更新去重缓存（同步函数，应在 executor 中运行）。

    将"下载网络请求"和"磁盘写入"合并到同一个 executor 任务中，
    避免 async 函数中直接进行同步文件 I/O 阻塞事件循环。

    Args:
        download_fn: 无参可调用对象，返回 PDF bytes 或 None
        filepath: 目标 PDF 路径
        filename: 文件名（用于去重缓存）
        output_dir: 输出目录（临时文件目录）

    Returns:
        PDF bytes（成功）或 None（失败）
    """
    pdf_data = download_fn()
    if not pdf_data:
        return None
    atomic_write(str(filepath), pdf_data, dir_=str(output_dir))
    # 延迟导入避免循环依赖
    from app.dedup import add_to_existing_files_cache
    add_to_existing_files_cache(filename)
    return pdf_data
