"""scanner.preview — 浏览器启动与预览转 PDF"""

import asyncio
import os
import time
import logging
import re
import tempfile
import shutil
import math
from contextlib import asynccontextmanager

from config.settings import (
    CAPTCHA_BASE, BROWSER_CHANNELS, http_client,
)
from app.captcha import solve_captcha

_log = logging.getLogger('std_scraper')

# ==================== 常量 ====================
_PREVIEW_CFG_CACHE_TTL = 10.0       # 预览质量配置缓存（秒）
_MAX_PREVIEW_RETRIES = 5            # 预览验证码最大重试次数
_DEFAULT_PAGE_W = 1190              # 默认页面宽度
_DEFAULT_PAGE_H = 1680              # 默认页面高度
_PUZZLE_GRID = 10                   # 拼图块网格（10x10）
_PDF_DPI = 168                      # 合成 PDF 的 DPI
_PREVIEW_DEFAULT_QUALITY = 0.6      # 预览质量默认值

# 预览质量缓存
_preview_quality_cache = None
_preview_quality_cache_ts = 0.0


def _get_preview_quality() -> float:
    """获取预览 PDF 缩放比例（带短时缓存）"""
    global _preview_quality_cache, _preview_quality_cache_ts
    now = time.monotonic()
    if _preview_quality_cache is not None and (now - _preview_quality_cache_ts) < _PREVIEW_CFG_CACHE_TTL:
        return _preview_quality_cache
    try:
        from config.manager import load_config
        q = load_config().get('download', {}).get('preview_quality', _PREVIEW_DEFAULT_QUALITY)
        q = max(0.3, min(1.0, float(q)))
    except Exception as e:
        _log.debug(f"读取预览质量配置失败，使用默认值: {e}")
        q = _PREVIEW_DEFAULT_QUALITY
    _preview_quality_cache = q
    _preview_quality_cache_ts = now
    return q

try:
    from PIL import Image
    from playwright.async_api import async_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


async def launch_browser():
    """按需启动浏览器，Chrome → Edge 降级"""
    if not PLAYWRIGHT_AVAILABLE:
        _log.warning("[PREVIEW] playwright 未安装，预览功能不可用")
        return None, None
    pw = await async_playwright().__aenter__()
    last_err = None
    for channel in BROWSER_CHANNELS:
        try:
            browser = await pw.chromium.launch(
                channel=channel, headless=True,
                args=['--no-sandbox', '--disable-gpu'],
            )
            ctx = await browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                locale='zh-CN', viewport={'width': 1400, 'height': 900},
            )
            return pw, ctx
        except Exception as e:
            last_err = e
            continue
    await pw.__aexit__(None, None, None)
    raise RuntimeError(f"Chrome/Edge 均不可用: {last_err}")


@asynccontextmanager
async def browser_session():
    """Playwright 浏览器会话上下文管理器，自动关闭。

    用法:
        async with browser_session() as ctx:
            if ctx is not None:
                await preview_to_pdf(hcno, filepath, ctx)
    """
    playwright_mgr, ctx = await launch_browser()
    try:
        yield ctx
    finally:
        if playwright_mgr:
            try:
                await playwright_mgr.__aexit__(None, None, None)
            except Exception as e:
                _log.debug(f"Playwright 关闭异常: {e}")


def _compose_page_images(page_data, tmp_dir, cookie_str, preview_url, quality):
    """同步：下载背景图 + 拼图块合成 → 返回页面 Image 列表。

    将网络下载 + CPU 密集的 PIL 处理合并到同一个 executor 任务中，
    避免 async 函数中直接进行同步 I/O 与 CPU 计算阻塞事件循环。
    """
    page_images = []
    for page_idx, pd in enumerate(page_data):
        bg_file = pd.get('bg', '')
        if not bg_file:
            continue

        style = pd.get('style', '')
        size_match = re.findall(r'\d+', style)
        pw = int(size_match[0]) if len(size_match) >= 1 else _DEFAULT_PAGE_W
        ph = int(size_match[1]) if len(size_match) >= 2 else _DEFAULT_PAGE_H

        bg_url = f"{CAPTCHA_BASE}/{bg_file}"
        try:
            bg_data = http_client.get(bg_url, headers={
                'Cookie': cookie_str,
                'Referer': preview_url,
            }).content
        except Exception as e:
            _log.debug(f"  背景图下载失败(p{page_idx}): {e}")
            continue

        tmp_bg = os.path.join(tmp_dir, f"bg_{page_idx}.jpg")
        with open(tmp_bg, 'wb') as f:
            f.write(bg_data)

        bg_img = None
        try:
            bg_img = Image.open(tmp_bg)
            canvas = Image.new('RGB', (pw, ph), '#ffffff')
            slice_w = math.ceil(pw / _PUZZLE_GRID)
            slice_h = math.ceil(ph / _PUZZLE_GRID)

            for span in pd.get('spans', []):
                cls = span.get('cls', '')
                parts = cls.split('-')
                if len(parts) < 3:
                    continue
                row = int(parts[1])
                col = int(parts[2])

                bg_style = span.get('style', '')
                pos_match = re.findall(r'\d+', bg_style)
                if len(pos_match) < 2:
                    continue
                bg_x = int(pos_match[0])
                bg_y = int(pos_match[1])

                right = min(bg_x + slice_w, bg_img.width)
                bottom = min(bg_y + slice_h, bg_img.height)
                if right <= bg_x or bottom <= bg_y:
                    continue
                crop = bg_img.crop((bg_x, bg_y, right, bottom))
                paste_x = row * slice_w
                paste_y = col * slice_h
                if paste_x + crop.width > pw:
                    paste_x = max(0, pw - crop.width)
                if paste_y + crop.height > ph:
                    paste_y = max(0, ph - crop.height)
                canvas.paste(crop, (paste_x, paste_y))

            canvas = canvas.resize(
                (int(pw * quality), int(ph * quality)),
                Image.LANCZOS,
            )
            page_images.append(canvas.convert('RGB'))

        except Exception as e:
            _log.debug(f"      拼图错误(p{page_idx}): {str(e)[:40]}")
        finally:
            if bg_img:
                bg_img.close()
    return page_images


def _save_page_images_as_pdf(page_images, filepath):
    """同步：将页面 Image 列表保存为 PDF，并释放内存。"""
    page_images[0].save(filepath, save_all=True, append_images=page_images[1:],
                       resolution=_PDF_DPI, format='PDF')
    for img in page_images:
        img.close()
    page_images.clear()


async def preview_to_pdf(hcno, filepath, browser_context):
    """通过在线预览获取 PDF：下载背景图 → 拼图块 → 合成 PDF"""
    if browser_context is None:
        _log.debug("[PREVIEW] 无浏览器上下文，跳过预览")
        return None
    pg = await browser_context.new_page()
    tmp_dir = None

    try:
        for retry in range(_MAX_PREVIEW_RETRIES):
            preview_url = f"{CAPTCHA_BASE}/showGb?type=online&hcno={hcno}"
            await pg.goto(preview_url, wait_until='load', timeout=30000)
            await pg.wait_for_timeout(2000)

            captcha_src = await pg.evaluate('() => document.querySelector(".verifyCode")?.src || ""')
            if not captcha_src:
                break

            captcha_data = await pg.evaluate('''async (src) => {
                const r = await fetch(src); const blob = await r.blob();
                return new Promise(resolve => {
                    const reader = new FileReader();
                    reader.onload = () => resolve(Array.from(new Uint8Array(reader.result)));
                    reader.readAsArrayBuffer(blob);
                });
            }''', captcha_src)

            code = solve_captcha(bytes(captcha_data))
            if not code or len(code) < 4:
                continue

            await pg.fill('#verifyCode', code)
            await pg.click('.verify-btn')
            await pg.wait_for_timeout(3000)

            body = await pg.evaluate('() => document.body.innerText')
            if '验证码不正确' in body:
                continue

            page_data = await pg.evaluate('''() => {
                const divs = Array.from(document.querySelectorAll("div.page"));
                return divs.map(div => {
                    const bg = div.getAttribute("bg") || "";
                    const style = div.getAttribute("style") || "";
                    const spans = Array.from(div.querySelectorAll("span")).map(s => ({
                        cls: s.className || "",
                        style: s.getAttribute("style") || "",
                    }));
                    return { bg, style, spans };
                });
            }''')
            break
        else:
            await pg.close()
            return None

        if not page_data:
            await pg.close()
            return None

        cookies = await browser_context.cookies()
        cookie_str = '; '.join(f"{c['name']}={c['value']}" for c in cookies)

        tmp_dir = tempfile.mkdtemp()
        quality = _get_preview_quality()

        # PIL 拼图合成（网络下载 + CPU 密集）整体放到 executor 执行
        loop = asyncio.get_running_loop()
        page_images = await loop.run_in_executor(
            None, _compose_page_images,
            page_data, tmp_dir, cookie_str, preview_url, quality)

        if page_images:
            await loop.run_in_executor(
                None, _save_page_images_as_pdf, page_images, filepath)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return True

        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None

    except Exception as e:
        _log.warning(f"      预览失败: {str(e)[:50]}")
        return None
    finally:
        if tmp_dir and os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)
        await pg.close()
