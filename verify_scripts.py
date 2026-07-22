"""
验证脚本 — 检查所有模块的导入和语法
"""
import sys
import os
import ast
from pathlib import Path

# 确保 stdout/stderr 使用 UTF-8 编码（Windows CI 环境默认 cp1252 无法输出中文）
if sys.platform == 'win32':
    os.environ.setdefault('PYTHONUTF8', '1')
    for stream_name in ('stdout', 'stderr'):
        stream = getattr(sys, stream_name)
        if hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(encoding='utf-8', errors='replace')
            except Exception:
                pass

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

ok, fail = 0, 0

def check_file(name):
    """检查文件是否存在"""
    path = BASE_DIR / name
    if path.exists():
        return str(path)
    print(f"  [WARN] 文件不存在: {name}")
    return None

def verify_syntax(filename):
    """验证 Python 文件语法"""
    global ok, fail
    path = check_file(filename)
    if not path:
        fail += 1
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            ast.parse(f.read())
        print(f"  OK  {filename} 语法正确")
        ok += 1
    except SyntaxError as e:
        print(f"  FAIL {filename}: {e}")
        fail += 1

def verify_import(module_name, symbols=None):
    """验证模块导入"""
    global ok, fail
    try:
        mod = __import__(module_name, fromlist=symbols or ["*"])
        if symbols:
            for sym in symbols:
                getattr(mod, sym)
        print(f"  OK  {module_name} 导入成功")
        ok += 1
    except Exception as e:
        print(f"  FAIL {module_name}: {e}")
        fail += 1

print("标准速递 — 模块验证")
print("=" * 50)

# 纯导入验证
verify_import("version", ["VERSION", "APP_NAME"])
verify_import("config.paths", ["CONFIG_DIR", "DB_PATH", "DATA_FILE", "CKPT_FILE", "migrate_old_data"])
verify_import("app.helpers", ["setup_logger", "get_logger", "safe_filename", "format_duration", "validate_path", "normalize_code", "atomic_write"])
verify_import("app.keywords", ["load_keywords", "is_safety", "is_aq_yj", "clean_name"])
verify_import("app.captcha", ["solve_captcha"])
verify_import("config.settings", ["OUTPUT_DIR", "DELAY", "HB_CODE_MAP", "HB_SAFETY_CODES", "http_client", "get_output_dir"])
verify_import("config.manager", ["load_config", "save_config", "DEFAULT_CONFIG", "deep_merge", "validate_config", "mask_sensitive_config"])

# 语法验证（耗时模块用语法检查代替导入）
verify_import("app.scanner", ["scan_pages", "download_with_captcha", "download_hb_with_captcha", "make_filename", "compare_snapshot"])
for sub in ["preview", "utils", "checkpoint", "progress", "download", "gb_scan", "search", "hb_scan", "db_scan", "tt_scan", "mem_scan", "quick", "change_tracker"]:
    verify_syntax(f"app/scanner/{sub}.py")
verify_syntax("app/scanner_engine.py")
verify_syntax("app/server.py")
verify_syntax("app/dedup.py")
verify_syntax("app/database.py")
verify_syntax("app/managers.py")
verify_syntax("app/notifier.py")
verify_syntax("app/routes/sse.py")
verify_syntax("app/routes/checkpoint.py")
verify_syntax("main.py")

print("=" * 50)
print(f"结果: {ok} 通过, {fail} 失败")
if fail > 0:
    sys.exit(1)
