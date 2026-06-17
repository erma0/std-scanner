"""
标准速递 - 桌面应用主入口
集成 pywebview + 系统托盘
"""
import os
import sys
import time
import asyncio
import signal
import threading
import webbrowser
import httpx
from pathlib import Path

# 添加当前目录到路径
sys.path.insert(0, str(Path(__file__).parent))

import app.server as api_module

try:
    import webview
    from pystray import Icon, Menu, MenuItem
    from PIL import Image
    import uvicorn
except ImportError as e:
    print(f"[ERROR] 缺少依赖: {e}")
    print("请安装: pip install -r requirements.txt")
    sys.exit(1)

# 全局变量
webview_window = None
api_server = None
_shutting_down = False

from config.settings import SERVER_HOST as _SERVER_HOST, SERVER_PORT as _SERVER_PORT


class Api:
    def open_folder_dialog(self):
        result = webview_window.create_file_dialog(
            webview.FileDialog.FOLDER,
            directory=""
        )
        if result and len(result) > 0:
            return result[0]
        return None

    def minimize(self):
        if webview_window:
            webview_window.minimize()

    def close_window(self):
        if webview_window:
            webview_window.destroy()


def wait_for_api(max_retries=30, interval=0.5):
    for i in range(max_retries):
        try:
            resp = httpx.get(f'http://{_SERVER_HOST}:{_SERVER_PORT}/api/health', timeout=1.0)
            if resp.status_code == 200:
                print(f"[INFO] API服务已就绪 (等待{i * interval:.1f}秒)")
                return True
        except Exception:
            pass
        time.sleep(interval)
    print("[WARN] API服务启动超时，继续尝试...")
    return False


def start_api_server():
    """启动FastAPI服务（使用 uvicorn.Server 以支持外部关闭）"""
    global api_server
    config = uvicorn.Config(
        "app.server:app",
        host=_SERVER_HOST,
        port=_SERVER_PORT,
        log_level="info",
        timeout_graceful_shutdown=0,
    )
    api_server = uvicorn.Server(config)
    api_server.run()


def create_tray_icon():
    """创建系统托盘图标"""
    # 加载应用 logo 作为托盘图标
    icon_path = Path(__file__).parent / "static" / "icon.ico"
    try:
        image = Image.open(icon_path)
    except Exception:
        image = Image.new('RGB', (64, 64), color=(59, 130, 246))

    def on_quit(icon, item):
        _shutdown()
        icon.stop()

    def on_show(icon, item):
        """显示窗口"""
        if webview_window:
            webview_window.show()

    def on_hide(icon, item):
        """隐藏窗口"""
        if webview_window:
            webview_window.hide()

    def on_open_browser(icon, item):
        """在浏览器中打开"""
        webbrowser.open(f'http://localhost:{_SERVER_PORT}/')

    menu = Menu(
        MenuItem('显示窗口', on_show),
        MenuItem('隐藏窗口', on_hide),
        MenuItem('在浏览器中打开', on_open_browser),
        MenuItem('退出', on_quit)
    )

    icon = Icon("标准速递", image, "标准速递", menu)
    return icon


def _shutdown():
    """统一关闭流程：通知 uvicorn 退出 → 关闭窗口"""
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True

    if api_server:
        api_server.should_exit = True
    if webview_window:
        try:
            webview_window.destroy()
        except Exception:
            pass


def on_window_closed():
    """窗口关闭事件：隐藏到托盘而非退出"""
    global webview_window
    if _shutting_down:
        return
    if webview_window:
        try:
            webview_window.hide()
        except Exception:
            pass


def _kill_port_occupant():
    """检测并杀掉占用端口的旧进程"""
    import subprocess
    try:
        result = subprocess.run(
            ['netstat', '-ano'], capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            if f':{_SERVER_PORT}' in line and 'LISTENING' in line:
                parts = line.split()
                pid = parts[-1]
                if pid.isdigit() and int(pid) != os.getpid():
                    print(f"[INFO] 端口 {_SERVER_PORT} 被进程 {pid} 占用，正在终止...")
                    subprocess.run(['taskkill', '/PID', pid, '/F'], capture_output=True, timeout=5)
                    time.sleep(1)
                    return True
    except Exception:
        pass
    return False


def main():
    global webview_window

    # 启动前检测端口占用
    _kill_port_occupant()

    def _signal_handler(sig, frame):
        _shutdown()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # === 并行启动：API 后台线程 + 窗口即刻显示 loading 页 ===

    # 1. 后台启动 API 服务
    print("[INFO] 启动API服务...")
    api_thread = threading.Thread(target=start_api_server, daemon=True)
    api_thread.start()

    # 2. 窗口配置
    _icon_path = str(Path(__file__).parent / "static" / "icon.ico")
    loading_url = f'file:///{(Path(__file__).parent / "static" / "loading.html").as_posix()}'

    window_options = {
        'title': '标准速递',
        'width': 1050,
        'height': 800,
        'resizable': True,
        'min_size': (800, 600),
        'background_color': '#f8fafc',
        'frameless': True,
        'easy_drag': True,
    }

    # 3. 立即创建窗口（显示本地 loading 页，不等 API）
    print("[INFO] 创建窗口（加载页）...")
    webview_window = webview.create_window(
        **window_options,
        url=loading_url,
        js_api=Api()
    )
    webview_window.events.closed += on_window_closed

    # 4. 后台线程：等 API 就绪 → 注入 pywebview
    def _setup_when_api_ready():
        wait_for_api()
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            api_module.setup_pywebview(webview_window, loop)
            print("[INFO] pywebview 窗口已注入到 API 模块")
        except Exception as e:
            print(f"[WARN] 无法设置 pywebview 窗口到 API: {e}")
        # loading 页自己会跳转，这里不再手动 load_url
        print("[INFO] API 就绪，加载页将自动跳转到主界面")

    setup_thread = threading.Thread(target=_setup_when_api_ready, daemon=True)
    setup_thread.start()

    # 5. 创建系统托盘
    tray_icon = None
    try:
        tray_icon = create_tray_icon()
    except Exception as e:
        print(f"[WARN] 创建系统托盘失败: {e}")

    if tray_icon:
        tray_thread = threading.Thread(target=tray_icon.run, daemon=True)
        tray_thread.start()

    # 6. 启动 webview 主循环（窗口立即显示 loading 页）
    print("[INFO] 启动窗口...")
    try:
        webview.start(icon=_icon_path)
    except Exception as e:
        print(f"[ERROR] 窗口启动失败: {e}")

    _shutdown()

    # 等待 API 线程完成（lifespan 清理通常 < 0.5 秒）
    api_thread.join(timeout=2)
    if api_thread.is_alive():
        print("[WARN] API服务未在2秒内退出，强制终止")
    os._exit(0)


if __name__ == "__main__":
    main()
