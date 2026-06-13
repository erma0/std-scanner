"""scanner.preview — 浏览器启动与预览转 PDF"""

import asyncio
import os
import time
import logging
import re
import tempfile
import shutil
import math

from config.settings import (
    CAPTCHA_BASE, BROWSER_CHANNELS, http_client,
)
from app.captcha import solve_captcha

_log = logging.getLogger('std_scraper')

# 预览质量缓存
_preview_quality_cache = None
_preview_quality_cache_ts = 0.0


def _get_preview_quality() -> float:
    """获取预览 PDF 缩放比例（带短时缓存）"""
    global _preview_quality_cache, _preview_quality_cache_ts
    now = time.monotonic()
    if _preview_quality_cache is not None and (now - _preview_quality_cache_ts) < 10.0:
        return _preview_quality_cache
    try:
        from config.manager import load_config
        q = load_config().get('download', {}).get('preview_quality', 0.6)
        q = max(0.3, min(1.0, float(q)))
    except Exception:
        q = 0.6
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


async def preview_to_pdf(hcno, filepath, browser_context):
    """通过在线预览获取 PDF：下载背景图 → 拼图块 → 合成 PDF"""
    if browser_context is None:
        _log.debug("[PREVIEW] 无浏览器上下文，跳过预览")
        return None
    pg = await browser_context.new_page()
    tmp_dir = None

    try:
        for retry in range(5):
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

        page_images = []
        tmp_dir = tempfile.mkdtemp()

        for page_idx, pd in enumerate(page_data):
            bg_file = pd.get('bg', '')
            if not bg_file:
                continue

            style = pd.get('style', '')
            size_match = re.findall(r'\d+', style)
            pw = int(size_match[0]) if len(size_match) >= 1 else 1190
            ph = int(size_match[1]) if len(size_match) >= 2 else 1680

            bg_url = f"{CAPTCHA_BASE}/{bg_file}"
            try:
                loop = asyncio.get_running_loop()
                bg_data = await loop.run_in_executor(None, lambda: http_client.get(bg_url, headers={
                    'Cookie': cookie_str,
                    'Referer': preview_url,
                }).content)
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
                slice_w = math.ceil(pw / 10)
                slice_h = math.ceil(ph / 10)

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
                    (int(pw * _get_preview_quality()), int(ph * _get_preview_quality())),
                    Image.LANCZOS,
                )
                page_images.append(canvas.convert('RGB'))

            except Exception as e:
                _log.debug(f"      拼图错误(p{page_idx}): {str(e)[:40]}")
            finally:
                if bg_img:
                    bg_img.close()

        if page_images:
            page_images[0].save(filepath, save_all=True, append_images=page_images[1:],
                               resolution=168, format='PDF')
            # 释放所有页面 Image 对象的内存
            for img in page_images:
                img.close()
            page_images.clear()
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
