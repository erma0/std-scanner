"""
标准速递 - 通用工具模块
包含：日志系统、原子写入、配置管理等
"""
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from datetime import datetime


def setup_logger(name='std_scraper', log_level=logging.INFO, log_dir=None):
    """
    设置日志系统
    同时输出到文件和控制台
    """
    logger = logging.getLogger(name)
    logger.setLevel(log_level)
    
    # 避免重复添加处理器
    if logger.handlers:
        return logger
    
    # 日志格式
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # 文件处理器
    if log_dir:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f'std_scraper_{datetime.now().strftime("%Y%m%d")}.log'
        
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(log_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    return logger


# 全局日志实例
_logger = None


def get_logger():
    """获取全局日志实例"""
    global _logger
    if _logger is None:
        _logger = setup_logger()
    return _logger


# 统一 dash 字符 → 英文半角连字符（用于标准编号"GB 12345—2023"→"GB 12345-2023"等场景）
_DASH_NORMALIZE_TABLE = str.maketrans({
    '\u2014': '-',  # EM DASH —
    '\u2013': '-',  # EN DASH –
    '\u2015': '-',  # HORIZONTAL BAR ―
    '\uFF0D': '-',  # FULLWIDTH HYPHEN-MINUS －
    '\u2212': '-',  # MINUS SIGN −
    '\u2010': '-',  # HYPHEN ‐
    '\u2011': '-',  # NON-BREAKING HYPHEN ‑
    '\u2012': '-',  # FIGURE DASH ‒
})

# 统一空格字符 → 半角空格（用于文件名归一化）
# 全角空格/不间断空格/各种宽度空格 → 半角空格；零宽字符与方向标记直接删除
# 避免标题中混入这些字符导致文件名不一致、去重失效（如 MEM 规章标题前导空格案例）
_SPACE_NORMALIZE_TABLE = str.maketrans({
    '\u3000': ' ',  # 全角空格 IDEOGRAPHIC SPACE
    '\u00a0': ' ',  # 不间断空格 NO-BREAK SPACE
    '\u2000': ' ',  # EN QUAD
    '\u2001': ' ',  # EM QUAD
    '\u2002': ' ',  # EN SPACE
    '\u2003': ' ',  # EM SPACE
    '\u2004': ' ',  # THREE-PER-EM SPACE
    '\u2005': ' ',  # FOUR-PER-EM SPACE
    '\u2006': ' ',  # SIX-PER-EM SPACE
    '\u2007': ' ',  # FIGURE SPACE
    '\u2008': ' ',  # PUNCTUATION SPACE
    '\u2009': ' ',  # THIN SPACE
    '\u200a': ' ',  # HAIR SPACE
    '\u200b': '',   # 零宽空格 ZERO WIDTH SPACE
    '\u200c': '',   # 零宽不连字 ZERO WIDTH NON-JOINER
    '\u200d': '',   # 零宽连字 ZERO WIDTH JOINER
    '\u200e': '',   # 从左至右标记 LEFT-TO-RIGHT MARK
    '\u200f': '',   # 从右至左标记 RIGHT-TO-LEFT MARK
    '\ufeff': '',   # BOM / ZERO WIDTH NO-BREAK SPACE
})

# 半角标点 → 全角（中文文件名一致性；同时覆盖 Windows 非法字符，避免被替换为下划线）
# 例如 '第5部分: 智能协同巡检' → '第5部分： 智能协同巡检'
_PUNCT_NORMALIZE_TABLE = str.maketrans({
    '(': '（', ')': '）',
    ':': '：', '?': '？', '*': '＊', '"': '＂',
    '<': '＜', '>': '＞', '|': '｜', '\\': '＼',
})

# 源站偶发把数字 1 录成字母 l/I（如 hbba 的 'GA/T 2075.l-2023'、"第l部分"），
# 仅在数字语境修正（小数点后的部分号、"第X部分"），避免误伤正常字母
_CODE_CONFUSION_RE = re.compile(r'(?<=\.)\s*[lI](?=$|[\s\-])')
_NAME_CONFUSION_RE = re.compile(r'第\s*[lI]\s*部分')


def normalize_code(code):
    """归一化标准编号：清理 sacinfo 标签 + 统一 dash 字符

    API 返回的 C_STD_CODE 可能包含 <sacinfo>...</sacinfo> HTML 标签
    （如 'GB/T <sacinfo>47334-2026</sacinfo>'），需清理。
    同时将 — 等统一为英文半角 -。

    在所有 code 提取点（GB/HB/DB/Search/ChangeTracker）调用，
    确保存入数据库/JSON 的 code 字段格式统一。
    """
    if not code:
        return code
    code = code.replace('<sacinfo>', '').replace('</sacinfo>', '')
    code = code.translate(_DASH_NORMALIZE_TABLE)
    # 源站偶发把部分号 1 录成 l/I（如 'GA/T 2075.l-2023'）
    return _CODE_CONFUSION_RE.sub('1', code)


def safe_filename(filename):
    """清理文件名，避免特殊字符问题和路径遍历

    归一化规则（生成与去重匹配共用同一口径）：
    1. dash 变体统一为半角 -，各类空格统一为半角空格；
    2. 控制字符（换行/回车/制表符等）转空格——直接删除会让前后词粘连；
    3. 半角标点转全角（括号/冒号等），Windows 非法字符用全角形式规避，
       不再替换为下划线（如 '第5部分：智能协同巡检' 而非 '第5部分_智能协同巡检'）；
    4. / 仍直接删除（保持 GB/T → GBT 的既有约定）；
    5. 连续横杠折叠为一个（'规则--液压电梯' → '规则-液压电梯'）；
    6. 连续空格压缩为一个，前后空格去除。
    """
    if not filename:
        return 'unnamed'

    # 统一所有 dash 变体为英文半角连字符
    filename = filename.translate(_DASH_NORMALIZE_TABLE)

    # 统一空格字符：全角空格/不间断空格/各种宽度空格 → 半角空格；零宽字符直接删除
    # 避免标题中混入这些字符导致文件名不一致、去重失效
    filename = filename.translate(_SPACE_NORMALIZE_TABLE)

    # 控制字符（换行符、回车、制表符等）转空格 — 搜索结果可能包含 \n，
    # Windows 文件名不允许这些字符，直接删除会让前后词粘连
    filename = ''.join(c if c >= ' ' else ' ' for c in filename)

    # 半角标点转全角（含 Windows 非法字符的规避），保留可读性
    filename = filename.translate(_PUNCT_NORMALIZE_TABLE)

    # 移除 /（不替换为全角，保持 GB/T → GBT 的既有约定）
    filename = filename.replace('/', '')

    # 源站偶发把"第1部分"录成"第l部分"/"第I部分"（如 GA/T 2075.l-2023）
    filename = _NAME_CONFUSION_RE.sub('第1部分', filename)
    # 编号中的部分号同样修正（'GA/T 2075.l-2023' → 'GA/T 2075.1-2023'）
    filename = _CODE_CONFUSION_RE.sub('1', filename)

    # 移除路径遍历
    while '..' in filename:
        filename = filename.replace('..', '__')

    # 折叠连续横杠（含 em dash 归一化后产生的 '--'，如 '规则——液压电梯'）
    while '--' in filename:
        filename = filename.replace('--', '-')

    # 压缩连续空格为单个空格（避免标准名称中多空格导致文件名混乱）
    while '  ' in filename:
        filename = filename.replace('  ', ' ')

    # 去除前后空格（避免文件名以空格开头/结尾导致去重失效）
    # Windows 也会自动去除文件名末尾空格，显式 strip 保持一致
    filename = filename.strip()

    # 限制长度
    if len(filename) > 150:
        name, ext = Path(filename).stem, Path(filename).suffix
        filename = f'{name[:140]}{ext}'

    return filename


def format_bytes(size_bytes):
    """格式化字节大小"""
    if size_bytes < 1024:
        return f'{size_bytes} B'
    elif size_bytes < 1024 * 1024:
        return f'{size_bytes / 1024:.1f} KB'
    else:
        return f'{size_bytes / 1024 / 1024:.1f} MB'


def format_duration(seconds):
    """格式化时间长度"""
    if seconds < 60:
        return f'{seconds:.1f}秒'
    elif seconds < 3600:
        minutes = int(seconds // 60)
        secs = int(seconds % 60)
        return f'{minutes}分{secs}秒'
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f'{hours}小时{minutes}分'


def validate_path(filepath, base_dir=None):
    """
    验证文件路径是否安全，防止路径遍历攻击
    返回规范化后的绝对路径，如果无效则返回 None
    """
    if not filepath:
        return None
    
    try:
        path = Path(filepath)
        normalized = path.resolve()
        
        if base_dir:
            base = Path(base_dir).resolve()
            if base not in normalized.parents and normalized != base:
                return None
        
        return str(normalized)
    except (OSError, ValueError):
        return None


def atomic_write(filepath, data, mode='wb', encoding=None, dir_=None):
    """原子写入文件，防止崩溃导致残缺文件。

    先写入临时文件，再 os.replace 替换目标文件。
    如果中途崩溃，临时文件会被丢弃，目标文件保持原样。

    Args:
        filepath: 目标文件路径（str 或 Path）
        data: 要写入的数据（bytes 或 str）
        mode: 写入模式，'wb'（二进制）或 'w'（文本）
        encoding: 文本模式编码，默认 'utf-8'
        dir_: 临时文件目录，默认为目标文件所在目录
    """
    filepath = str(filepath)
    if dir_ is None:
        dir_ = os.path.dirname(filepath) or '.'
    if encoding is None:
        encoding = 'utf-8'

    suffix = os.path.splitext(filepath)[1] or '.tmp'
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix, dir=dir_)
    try:
        if 'b' in mode:
            with os.fdopen(tmp_fd, mode) as f:
                f.write(data)
        else:
            with os.fdopen(tmp_fd, mode, encoding=encoding) as f:
                f.write(data)
        os.replace(tmp_path, filepath)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
