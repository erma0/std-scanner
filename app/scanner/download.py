"""scanner.download — 统一验证码下载

国标下载流程 (openstd.samr.gov.cn/bzgk/std/):
1. GET showGb?type=download&hcno=xxx  (建立 session)
2. GET gc?_timestamp  (获取验证码图片)
3. POST verifyCode verifyCode=ABCD  (验证)
4. GET viewGb?hcno=xxx  (下载 PDF)
"""

import time
import logging

import httpx

from config.settings import GB_DOWNLOAD_BASE, DELAY, get_captcha_client
from app.captcha import solve_captcha

_log = logging.getLogger('std_scraper')

_NETWORK_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
)


def _unified_captcha_download(dl_config, max_ocr_retries=12, max_network_retries=3):
    """统一的验证码下载流程。

    区分三类错误：
    - OCR 错误（验证码识别失败/验证失败）：在 max_ocr_retries 内重试
    - 网络错误（超时/连接断开/协议错误）：独立计数 max_network_retries
    - PDF 错误（验证通过但 PDF 获取失败，如 session 过期）：重建 session 后重试

    Args:
        dl_config: dict，包含：
            site_type: 'gb' | 'hb' | 'db'
            captcha_getter: callable(client) → captcha_image_bytes
            captcha_verifier: callable(client, code) → bool
            pdf_getter: callable(client) → pdf_bytes | None
        max_ocr_retries: OCR 验证码最大重试次数
        max_network_retries: 网络错误最大重试次数（独立计数）
    Returns:
        pdf_data (bytes) or None
    """
    site_type = dl_config['site_type']
    client = get_captcha_client(site_type)

    network_failures = 0
    pdf_failures = 0
    max_pdf_retries = 3
    ocr_attempts = 0

    while ocr_attempts < max_ocr_retries:
        ocr_attempts += 1
        try:
            captcha_data = dl_config['captcha_getter'](client)
            code = solve_captcha(captcha_data)
            if not code or len(code) < 4:
                time.sleep(DELAY)
                continue

            if not dl_config['captcha_verifier'](client, code):
                time.sleep(DELAY)
                continue

            pdf_data = dl_config['pdf_getter'](client)
            if pdf_data and len(pdf_data) > 500 and pdf_data[:5] == b'%PDF-':
                return pdf_data

            # 验证码通过但 PDF 获取失败 → 可能是 session 过期
            # 重建客户端 session 后重试
            pdf_failures += 1
            if pdf_failures <= max_pdf_retries:
                _log.debug(f"[DL] PDF获取失败(#{pdf_failures})，重建session重试")
                client.cookies.clear()
                time.sleep(DELAY)
                continue
            return None

        except _NETWORK_EXCEPTIONS as e:
            network_failures += 1
            if network_failures > max_network_retries:
                _log.warning(f"[DL] 网络重试耗尽 ({network_failures}/{max_network_retries}): {e}")
                return None
            _log.debug(f"[DL] 网络错误，重试 {network_failures}/{max_network_retries}: {e}")
            time.sleep(DELAY * 2)
            ocr_attempts -= 1
            continue

        except Exception as e:
            _log.debug(f"[DL] 验证码下载尝试 {ocr_attempts}/{max_ocr_retries} 失败: {e}")
            time.sleep(DELAY)

    return None


# ==================== 国标 (GB) 下载 - openstd.samr.gov.cn 新流程 ====================
def _gb_show_gb(client, hcno):
    """第一步：访问 showGb 页面建立 session（会写入 session cookie）"""
    return client.get(
        f"{GB_DOWNLOAD_BASE}/showGb",
        params={'type': 'download', 'hcno': hcno, 'request_locale': 'zh'},
    )


def _gb_captcha_getter(client, hcno):
    """获取国标验证码图片（新版接口）

    必须先调用 showGb 建立 session，否则验证码会验证失败
    """
    # 1. 先访问 showGb 建立 session
    show_resp = _gb_show_gb(client, hcno)
    show_resp.raise_for_status()
    # 2. 获取验证码图片
    captcha_resp = client.get(
        f"{GB_DOWNLOAD_BASE}/gc",
        params={'_': int(time.time() * 1000)},
    )
    captcha_resp.raise_for_status()
    return captcha_resp.content


def _gb_captcha_verifier(client, code):
    """验证国标验证码（返回 'success' / 'error'）"""
    resp = client.post(
        f"{GB_DOWNLOAD_BASE}/verifyCode",
        data={'verifyCode': code},
        headers={'X-Requested-With': 'XMLHttpRequest'},
    )
    return resp.text.strip() == 'success'


def _gb_pdf_getter(client, hcno):
    """获取国标 PDF 数据

    注意：必须在 verifyCode 成功返回 'success' 后立即请求，否则会丢失 session

    Returns:
        bytes: PDF 内容
        None: 非 PDF 响应（验证码过期/服务器异常等）
    """
    resp = client.get(f"{GB_DOWNLOAD_BASE}/viewGb", params={'hcno': hcno})
    ct = resp.headers.get('content-type', '')
    if 'pdf' in ct.lower() or (resp.content[:5] == b'%PDF-'):
        return resp.content
    _log.debug(f"[GB-DL] viewGb 返回非 PDF (ct={ct}, len={len(resp.content)})")
    return None


def download_with_captcha(hcno):
    """通过验证码下载国标 PDF（适配 openstd.samr.gov.cn 新接口）

    流程：
    1. showGb?type=download&hcno=xxx  (建立 session)
    2. gc?_t=xxx  (获取验证码图片)
    3. verifyCode  (POST 验证)
    4. viewGb?hcno=xxx  (下载 PDF)
    """
    return _unified_captcha_download({
        'site_type': 'gb',
        'captcha_getter': lambda client: _gb_captcha_getter(client, hcno),
        'captcha_verifier': _gb_captcha_verifier,
        'pdf_getter': lambda client: _gb_pdf_getter(client, hcno),
    })
