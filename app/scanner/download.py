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

from config.settings import GB_DOWNLOAD_BASE, get_delay, get_captcha_client
from app.captcha import solve_captcha

_log = logging.getLogger('std_scraper')

_NETWORK_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
)

# 默认重试次数（与 AGENTS.md 中"8次OCR重试"对齐：12 容错更充分）
DEFAULT_MAX_OCR_RETRIES = 12
DEFAULT_MAX_NETWORK_RETRIES = 3
DEFAULT_MAX_PDF_RETRIES = 3


def _unified_captcha_download(dl_config, max_ocr_retries=DEFAULT_MAX_OCR_RETRIES, max_network_retries=DEFAULT_MAX_NETWORK_RETRIES, client=None):
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
        client: 可选，外部传入的独立 httpx.Client（并发场景使用）。
                None 时使用全局共享 client。传入时调用方负责关闭。
    Returns:
        pdf_data (bytes) or None
    """
    site_type = dl_config['site_type']
    # client=None → 用共享 client（向后兼容）；并发场景由调用方传入独立 client
    own_client = client is not None
    if client is None:
        client = get_captcha_client(site_type)

    network_failures = 0
    pdf_failures = 0
    max_pdf_retries = DEFAULT_MAX_PDF_RETRIES
    ocr_attempts = 0
    # 累计失败原因统计（用于最终汇总日志）
    fail_stats = {'ocr_empty': 0, 'verify_fail': 0, 'pdf_fail': 0, 'exception': 0}

    try:
        while ocr_attempts < max_ocr_retries:
            ocr_attempts += 1
            try:
                captcha_data = dl_config['captcha_getter'](client)
                code = solve_captcha(captcha_data)
                if not code or len(code) < 4:
                    fail_stats['ocr_empty'] += 1
                    _log.debug(f"[DL-{site_type}] OCR 返回空/过短: '{code}' (尝试 {ocr_attempts}/{max_ocr_retries})")
                    time.sleep(get_delay())
                    continue

                if not dl_config['captcha_verifier'](client, code):
                    fail_stats['verify_fail'] += 1
                    _log.debug(f"[DL-{site_type}] 验证码校验失败: '{code}' (尝试 {ocr_attempts}/{max_ocr_retries})")
                    time.sleep(get_delay())
                    continue

                pdf_data = dl_config['pdf_getter'](client)
                if pdf_data and len(pdf_data) > 500 and pdf_data[:5] == b'%PDF-':
                    if ocr_attempts > 1 or pdf_failures > 0:
                        _log.info(f"[DL-{site_type}] 第 {ocr_attempts} 次尝试下载成功 (pdf_failures={pdf_failures})")
                    return pdf_data

                # 验证码通过但 PDF 获取失败 → 可能是 session 过期
                # 重建客户端 session 后重试
                pdf_failures += 1
                fail_stats['pdf_fail'] += 1
                if pdf_failures <= max_pdf_retries:
                    _log.info(f"[DL-{site_type}] PDF获取失败(#{pdf_failures})，重建session重试 (尝试 {ocr_attempts}/{max_ocr_retries})")
                    client.cookies.clear()
                    time.sleep(get_delay())
                    continue
                _log.warning(f"[DL-{site_type}] PDF 重试耗尽 ({pdf_failures}/{max_pdf_retries})")
                return None

            except _NETWORK_EXCEPTIONS as e:
                network_failures += 1
                if network_failures > max_network_retries:
                    _log.warning(f"[DL-{site_type}] 网络重试耗尽 ({network_failures}/{max_network_retries}): {e}")
                    return None
                _log.debug(f"[DL-{site_type}] 网络错误，重试 {network_failures}/{max_network_retries}: {e}")
                time.sleep(get_delay() * 2)
                ocr_attempts -= 1
                continue

            except Exception as e:
                fail_stats['exception'] += 1
                _log.info(f"[DL-{site_type}] 验证码下载尝试 {ocr_attempts}/{max_ocr_retries} 异常: {e}")
                time.sleep(get_delay())

        _log.warning(f"[DL-{site_type}] 下载失败：OCR重试耗尽 ({ocr_attempts}/{max_ocr_retries}) "
                     f"统计: {fail_stats}")
        return None
    finally:
        # 仅关闭外部传入的独立 client；共享 client 由 close_captcha_clients 统一管理
        if own_client:
            try:
                client.close()
            except Exception:
                pass


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
    # 提高到 info 级别：验证码已通过但 PDF 拿不到，是诊断 GB 下载失败的关键信号
    body_preview = resp.content[:120]
    _log.info(f"[GB-DL] viewGb 返回非 PDF (ct={ct}, len={len(resp.content)}, body={body_preview!r})")
    return None


def download_with_captcha(hcno, client=None):
    """通过验证码下载国标 PDF（适配 openstd.samr.gov.cn 新接口）

    流程：
    1. showGb?type=download&hcno=xxx  (建立 session)
    2. gc?_t=xxx  (获取验证码图片)
    3. verifyCode  (POST 验证)
    4. viewGb?hcno=xxx  (下载 PDF)

    Args:
        hcno: 标准 hcno 标识
        client: 可选，外部传入的独立 httpx.Client（并发场景使用，调用方负责关闭）
    """
    return _unified_captcha_download({
        'site_type': 'gb',
        'captcha_getter': lambda c: _gb_captcha_getter(c, hcno),
        'captcha_verifier': _gb_captcha_verifier,
        'pdf_getter': lambda c: _gb_pdf_getter(c, hcno),
    }, client=client)
