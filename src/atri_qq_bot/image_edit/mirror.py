"""对称修图的纯图像操作：左半镜像覆盖右半，以竖直中轴为对称轴。

第一性原理：对称化只需要一次 crop + 一次水平翻转 + 一次 paste，
不需要任何模型调用；奇数宽度时中列恰好落在对称轴上保持不变。
"""

from __future__ import annotations

from PIL import Image


def mirror_left_to_right(image: Image.Image) -> Image.Image:
    """以竖直中轴为对称轴，把左半部分镜像后覆盖右半部分。

    宽度为奇数时，中间一列正好位于对称轴，保持原样（数学上正确的
    镜像对称）。宽度过小（<2 像素）没有可覆盖的右半，原样返回副本。
    动图只处理首帧，输出为静态图。
    """
    width, height = image.size
    if width < 2:
        return image.copy()

    mid = (width + 1) // 2
    left = image.crop((0, 0, mid, height))
    mirrored = left.transpose(Image.FLIP_LEFT_RIGHT)

    result = image.copy()
    result.paste(mirrored, (width - mid, 0))
    return result
