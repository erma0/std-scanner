"""
验证码识别模块 — ddddocr 封装

预处理流程（v3.8.2 增强）：
  1. 3x 放大（LANCZOS）
  2. 灰度转换
  3. 中值滤波去噪线（3x3 核）
  4. 形态学开运算去除小噪点
  5. 阈值 128 二值化
  6. ddddocr 识别 → 过滤非字母数字 → 转大写
"""
from io import BytesIO

import ddddocr
from PIL import Image, ImageFilter, ImageMorph

# ==================== 单例 OCR ====================
_ocr = None

# 形态学开运算查找表（3x3 十字核）：先腐蚀再膨胀，去除小噪点
_OPEN_LUT = None


def _get_open_lut():
    """获取形态学开运算查找表（延迟初始化）"""
    global _OPEN_LUT
    if _OPEN_LUT is None:
        try:
            # 3x3 十字核（4-邻域）
            _OPEN_LUT = ImageMorph.MorphOp(op_name='erode4').lut
        except Exception:
            _OPEN_LUT = None
    return _OPEN_LUT


def _get_ocr():
    """获取 ddddocr 单例"""
    global _ocr
    if _ocr is None:
        _ocr = ddddocr.DdddOcr(show_ad=False)
    return _ocr


def solve_captcha(img_data: bytes) -> str:
    """
    识别验证码图片，返回大写字母数字串。

    预处理流程：3x 放大 → 灰度 → 中值滤波 → 形态学开运算 → 二值化
    过滤：仅保留字母数字 → 大写 → 长度 < 4 返回空
    """
    try:
        img = Image.open(BytesIO(img_data))
        w, h = img.size
        # 1. 3x 放大
        img = img.resize((w * 3, h * 3), Image.LANCZOS)
        # 2. 灰度转换
        img = img.convert('L')
        # 3. 中值滤波去噪线（3x3 核，有效去除细线干扰）
        img = img.filter(ImageFilter.MedianFilter(size=3))
        # 4. 形态学开运算去除小噪点
        try:
            lut = _get_open_lut()
            if lut is not None:
                morph = ImageMorph.MorphOp(lut=lut)
                img = morph.apply(img)
        except Exception:
            pass
        # 5. 阈值二值化
        img = img.point(lambda p: 255 if p > 128 else 0)
        buf = BytesIO()
        img.save(buf, format='PNG')
        img_data = buf.getvalue()
        img.close()
    except Exception:
        pass  # 预处理失败则用原图

    raw = _get_ocr().classification(img_data).strip()
    code = ''.join(c.upper() for c in raw if c.isalnum())
    return code
