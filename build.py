"""
标准速递 — PyInstaller 编译脚本

用法:
    python build.py              # 编译为单目录模式（推荐，启动快）
    python build.py --onefile    # 编译为单文件模式（体积小，启动慢）
    python build.py --clean      # 清理后重新编译

输出:
    dist/标准速递/               # 目录模式
    dist/标准速递.exe            # 单文件模式
"""

import os
import sys
import shutil
import subprocess
from pathlib import Path

# 项目根目录
ROOT = Path(__file__).parent

# 从 version.py 读取版本号
def get_version():
    version_file = ROOT / "version.py"
    ns = {}
    exec(version_file.read_text(encoding="utf-8"), ns)
    return ns.get("__version__", "0.0.0")

VERSION = get_version()
APP_NAME = "标准速递"


def clean():
    """清理构建产物"""
    dirs_to_clean = ["build", "dist"]
    for d in dirs_to_clean:
        p = ROOT / d
        if p.exists():
            shutil.rmtree(p)
            print(f"[CLEAN] 已删除 {p}")

    # 清理 spec 文件
    for spec in ROOT.glob("*.spec"):
        spec.unlink()
        print(f"[CLEAN] 已删除 {spec}")

    print("[CLEAN] 清理完成")


def check_pyinstaller():
    """检查 PyInstaller 是否已安装"""
    try:
        import PyInstaller
        print(f"[OK] PyInstaller {PyInstaller.__version__}")
        return True
    except ImportError:
        print("[ERROR] PyInstaller 未安装，正在安装...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])
        return True


def build(onefile=False):
    """执行 PyInstaller 编译"""
    check_pyinstaller()

    # 收集数据文件
    datas = []

    # static 目录（图标、CSS、字体、loading.html）
    static_dir = ROOT / "static"
    if static_dir.exists():
        datas.append(str(static_dir))

    # ui.html
    ui_html = ROOT / "ui.html"
    if ui_html.exists():
        datas.append(str(ui_html))

    # ddddocr 的 onnx 模型文件
    try:
        import ddddocr
        ddddocr_dir = Path(ddddocr.__file__).parent
        model_file = ddddocr_dir / "common_old.onnx"
        if model_file.exists():
            datas.append(f"{model_file};ddddocr")
            print(f"[OK] 找到 ddddocr 模型: {model_file}")
        # 也检查 common.onnx
        model_file2 = ddddocr_dir / "common.onnx"
        if model_file2.exists():
            datas.append(f"{model_file2};ddddocr")
            print(f"[OK] 找到 ddddocr 模型: {model_file2}")
    except ImportError:
        print("[WARN] ddddocr 未安装，验证码功能将不可用")

    # 构建 datas 参数
    datas_args = []
    for d in datas:
        if ";" in d:
            # 已经是 src;dst 格式
            datas_args.extend(["--add-data", d])
        else:
            p = Path(d)
            if p.is_dir():
                datas_args.extend(["--add-data", f"{p};{p.name}"])
            else:
                datas_args.extend(["--add-data", f"{p};."])

    # 隐式导入（PyInstaller 无法自动检测的模块）
    hidden_imports = [
        "ddddocr",
        "onnxruntime",
        "PIL",
        "PIL.Image",
        "PIL.ImageFilter",
        "PIL.ImageMorph",
        "httpx",
        "httpx._transports",
        "httpx._transports.default",
        "parsel",
        "parsel.csstranslator",
        "parsel.selector",
        "parsel.utils",
        "lxml",
        "lxml._elementpath",
        "lxml.etree",
        "cssselect",
        "fastapi",
        "uvicorn",
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "webview",
        "webview._guilib",
        "pystray",
        "psutil",
        "watchfiles",
        "apscheduler",
        "apscheduler.schedulers",
        "apscheduler.schedulers.background",
        "apscheduler.triggers",
        "apscheduler.triggers.cron",
        "anyio",
        "anyio._backends",
        "anyio._backends._asyncio",
        "sniffio",
        "starlette",
        "starlette.routing",
        "starlette.middleware",
        "starlette.responses",
        "starlette.requests",
        "pydantic",
    ]

    hidden_args = []
    for mod in hidden_imports:
        hidden_args.extend(["--hidden-import", mod])

    # 构建命令
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", APP_NAME,
        "--noconfirm",
        "--console",  # 保留控制台窗口（方便调试，去掉则用 --noconsole）
        *datas_args,
        *hidden_args,
    ]

    # 图标
    icon_path = ROOT / "static" / "icon.ico"
    if icon_path.exists():
        cmd.extend(["--icon", str(icon_path)])

    if onefile:
        cmd.append("--onefile")
    else:
        cmd.append("--onedir")

    # 版本信息（Windows 版本资源）
    version_file = create_version_file()
    if version_file:
        cmd.extend(["--version-file", str(version_file)])

    # 入口脚本
    cmd.append(str(ROOT / "main.py"))

    print(f"\n{'='*60}")
    print(f"  {APP_NAME} v{VERSION} 编译")
    print(f"  模式: {'单文件' if onefile else '目录'}")
    print(f"{'='*60}\n")

    # 执行编译
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    result = subprocess.run(cmd, cwd=str(ROOT), env=env)

    # 清理临时文件
    if version_file and version_file.exists():
        version_file.unlink()

    if result.returncode != 0:
        print(f"\n[ERROR] 编译失败 (exit code: {result.returncode})")
        sys.exit(1)

    # 验证输出
    if onefile:
        exe_path = ROOT / "dist" / f"{APP_NAME}.exe"
    else:
        exe_path = ROOT / "dist" / APP_NAME / f"{APP_NAME}.exe"

    if exe_path.exists():
        size_mb = exe_path.stat().st_size / (1024 * 1024)
        print(f"\n{'='*60}")
        print(f"  编译成功!")
        print(f"  输出: {exe_path}")
        print(f"  大小: {size_mb:.1f} MB")
        print(f"  版本: v{VERSION}")
        print(f"{'='*60}")
    else:
        print(f"\n[ERROR] 编译产物未找到: {exe_path}")
        sys.exit(1)


def create_version_file():
    """生成 Windows 版本信息文件（.version）— 兼容 PyInstaller 6.x"""
    try:
        from PyInstaller.utils.win32.versioninfo import (
            VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable,
            StringStruct, VarFileInfo, VarStruct,
        )

        ver_parts = [int(x) for x in VERSION.split(".")]
        while len(ver_parts) < 4:
            ver_parts.append(0)

        vs = VSVersionInfo(
            ffi=FixedFileInfo(
                filevers=ver_parts,
                prodvers=ver_parts,
                mask=0x3f,
                flags=0x0,
                OS=0x40004,
                fileType=0x1,
                subtype=0x0,
                date=(0, 0),
            ),
            kids=[
                StringFileInfo(
                    [
                        StringTable(
                            "080404B0",
                            [
                                StringStruct("CompanyName", "Standard Scanner Team"),
                                StringStruct("FileDescription", "标准速递 - 安全标准扫描下载工具"),
                                StringStruct("FileVersion", VERSION),
                                StringStruct("InternalName", "std_scanner"),
                                StringStruct("LegalCopyright", "Copyright (c) 2024-2026"),
                                StringStruct("OriginalFilename", "标准速递.exe"),
                                StringStruct("ProductName", "标准速递"),
                                StringStruct("ProductVersion", VERSION),
                            ]
                        )
                    ]
                ),
                VarFileInfo([VarStruct("Translation", [2052, 1200])]),
            ],
        )

        version_file = ROOT / "build_version.txt"
        with open(str(version_file), "w", encoding="utf-8") as f:
            f.write(str(vs))
        print(f"[OK] 版本信息文件: {version_file}")
        return version_file
    except ImportError:
        print("[WARN] 非 Windows 平台，跳过版本信息文件")
        return None
    except Exception as e:
        print(f"[WARN] 生成版本信息文件失败: {e}")
        return None


def main():
    args = sys.argv[1:]
    onefile = "--onefile" in args
    do_clean = "--clean" in args

    if do_clean:
        clean()

    build(onefile=onefile)


if __name__ == "__main__":
    main()
