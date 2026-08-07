"""缩略图生成：纯图像处理模块。

输入原始图片字节，输出 WebP 缩略图字节。与存储层解耦——不关心字节
从哪来、写到哪去，由调用方（complete 路由）编排。

设计约束：
- 生成失败（损坏图片 / 像素超限 / 未知格式）一律返回 None，调用方降级，
  绝不让缩略图失败影响上传主流程（原图已 ready）。
- 只处理 image 类；audio/video/document 不做缩略图。
"""

import io
import logging

from PIL import Image, ImageOps, UnidentifiedImageError

logger = logging.getLogger(__name__)

# 缩略图最长边（px），列表展示足够；超出按比例缩小
THUMBNAIL_MAX_EDGE = 400
# 缩略图格式（现代浏览器均支持 WebP，体积最小）
THUMBNAIL_FORMAT = "WEBP"
THUMBNAIL_CONTENT_TYPE = "image/webp"
THUMBNAIL_QUALITY = 80
# 像素总量保护：超过则放弃生成（避免超大图拖垮 CPU，降级为图标占位）
THUMBNAIL_MAX_PIXELS = 50_000_000

# 已知的失败类型：损坏/截断图片、非法数据、Pillow 解压炸弹防护
_THUMBNAIL_FAILURE_TYPES = (
    UnidentifiedImageError,
    OSError,
    ValueError,
    Image.DecompressionBombError,
)


def generate_thumbnail(data: bytes, *, max_edge: int = THUMBNAIL_MAX_EDGE) -> bytes | None:
    """从原始图片字节生成 WebP 缩略图；任何异常返回 None。"""
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.width * img.height > THUMBNAIL_MAX_PIXELS:
                logger.warning("缩略图生成跳过（像素超限 %dx%d）", img.width, img.height)
                return None
            # 按 EXIF 旋转还原方向，再等比缩小
            img = ImageOps.exif_transpose(img)
            img.thumbnail((max_edge, max_edge))
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA")
            buf = io.BytesIO()
            img.save(buf, format=THUMBNAIL_FORMAT, quality=THUMBNAIL_QUALITY)
            return buf.getvalue()
    except _THUMBNAIL_FAILURE_TYPES as exc:
        logger.warning("缩略图生成失败（已降级）：%s", exc)
        return None


__all__ = [
    "THUMBNAIL_CONTENT_TYPE",
    "THUMBNAIL_FORMAT",
    "THUMBNAIL_MAX_EDGE",
    "generate_thumbnail",
]
