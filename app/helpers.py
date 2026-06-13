"""
标准速递 - 通用工具模块
包含：日志系统、原子写入、配置管理等
"""
import logging
import os
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


def normalize_code(code):
    """归一化标准编号中的 dash 字符，将 — 等统一为英文半角 -
    
    在所有 code 提取点（GB/HB/DB/Search/ChangeTracker）调用，
    确保存入数据库/JSON 的 code 字段格式统一。
    """
    if not code:
        return code
    return code.translate(_DASH_NORMALIZE_TABLE)


def safe_filename(filename):
    """清理文件名，避免特殊字符问题和路径遍历"""
    if not filename:
        return 'unnamed'
    
    # 统一所有 dash 变体为英文半角连字符
    filename = filename.translate(_DASH_NORMALIZE_TABLE)
    
    invalid_chars = '<>:"/\\|?*'
    for char in invalid_chars:
        filename = filename.replace(char, '_')
    
    # 移除路径遍历
    while '..' in filename:
        filename = filename.replace('..', '__')
    
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
