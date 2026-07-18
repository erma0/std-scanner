"""
验证码识别模块 — ddddocr 封装

多策略识别（v3.8.3 重构）：
  1. 原图识别（ddddocr 内部已做预处理，多数验证码原图效果最佳）
  2. 原图结果不足 4 字符时，依次尝试多种预处理方案，取最长结果：
     a. basic     — 灰度 + Otsu 自适应阈值（最温和，不破坏字符）
     b. enhanced  — 2x 放大 + 灰度 + Otsu 阈值（增强细节）
     c. denoise   — 2x 放大 + 灰度 + 轻度中值滤波 + Otsu 阈值（去噪线）

移除的破坏性步骤（v3.8.2 旧版）：
  - 3x 放大（过度放大反而损失字符结构）
  - 形态学开运算 erode4（腐蚀让细字符断裂）
  - 固定阈值 128（不适应不同亮度的验证码背景）
"""
import logging
import threading
from io import BytesIO

import ddddocr
from PIL import Image, ImageFilter

_log = logging.getLogger('std_scraper')

# ==================== 单例 OCR ====================
_ocr = None
_ocr_lock = threading.Lock()


def _get_ocr():
    """获取 ddddocr 单例（线程安全）"""
    global _ocr
    if _ocr is not None:
        return _ocr
    with _ocr_lock:
        if _ocr is None:
            _ocr = ddddocr.DdddOcr(show_ad=False)
    return _ocr


def _otsu_threshold(img_gray: Image.Image) -> int:
    """计算 Otsu 阈值（最大类间方差法，纯 PIL 实现，无 numpy 依赖）。

    自适应选择阈值，比固定 128 更能适应不同亮度的验证码背景。
    """
    hist = img_gray.histogram()  # 长度 256 的列表
    total = sum(hist)
    if total == 0:
        return 128
    sum_total = sum(i * hist[i] for i in range(256))
    sum_b = 0
    w_b = 0
    max_var = -1.0
    threshold = 128
    for i in range(256):
        w_b += hist[i]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += i * hist[i]
        mean_b = sum_b / w_b
        mean_f = (sum_total - sum_b) / w_f
        var_between = w_b * w_f * (mean_b - mean_f) ** 2
        if var_between > max_var:
            max_var = var_between
            threshold = i
    return threshold


def _binarize(img_gray: Image.Image) -> Image.Image:
    """Otsu 阈值二值化"""
    t = _otsu_threshold(img_gray)
    return img_gray.point(lambda p: 255 if p > t else 0)


def _preprocess_basic(img: Image.Image) -> Image.Image:
    """方案 A：灰度 + Otsu 阈值（最温和，不破坏字符）"""
    return _binarize(img.convert('L'))


def _preprocess_enhanced(img: Image.Image) -> Image.Image:
    """方案 B：2x 放大 + 灰度 + Otsu 阈值（增强细节）"""
    w, h = img.size
    img = img.resize((w * 2, h * 2), Image.LANCZOS).convert('L')
    return _binarize(img)


def _preprocess_denoise(img: Image.Image) -> Image.Image:
    """方案 C：2x 放大 + 灰度 + 轻度中值滤波 + Otsu 阈值（去噪线）

    中值滤波保留在 2x 放大之后做，避免小图时把字符一起滤掉。
    """
    w, h = img.size
    img = img.resize((w * 2, h * 2), Image.LANCZOS).convert('L')
    img = img.filter(ImageFilter.MedianFilter(size=3))
    return _binarize(img)


# 预处理策略（顺序即尝试顺序，basic 最温和放最前）
_PREPROCESSORS = (
    ('basic', _preprocess_basic),
    ('enhanced', _preprocess_enhanced),
    ('denoise', _preprocess_denoise),
)


def solve_captcha(img_data: bytes) -> str:
    """
    识别验证码图片，返回大写字母数字串。

    多策略并行尝试，取最长结果：
    1. 原图识别（ddddocr 内部已做预处理，多数验证码原图效果最佳）
    2. 原图结果 < 4 字符时，依次尝试 basic / enhanced / denoise 三种预处理方案，
       任一方案结果 >= 4 字符立即返回，否则取最长结果。

    过滤：仅保留字母数字 → 大写。
    """
    ocr = _get_ocr()
    best = ''

    # 策略 1：原图识别
    try:
        raw = ocr.classification(img_data).strip()
        code = ''.join(c.upper() for c in raw if c.isalnum())
        if len(code) >= 4:
            return code
        if len(code) > len(best):
            best = code
    except Exception as e:
        _log.debug(f"原图识别失败: {e}")

    # 策略 2-4：多种预处理方案，取最长结果
    try:
        with Image.open(BytesIO(img_data)) as img:
            for name, pp in _PREPROCESSORS:
                try:
                    processed = pp(img)
                    buf = BytesIO()
                    processed.save(buf, format='PNG')
                    raw = ocr.classification(buf.getvalue()).strip()
                    code = ''.join(c.upper() for c in raw if c.isalnum())
                    if len(code) > len(best):
                        best = code
                        if len(best) >= 4:
                            return best
                except Exception as e:
                    _log.debug(f"预处理 {name} 识别失败: {e}")
    except Exception as e:
        _log.debug(f"图像预处理失败: {e}")

    return best
