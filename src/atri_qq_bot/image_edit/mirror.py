"""对称修图的纯图像操作：四方向镜像，以图像中轴为对称轴。

第一性原理：对称化只需要一次 crop + 一次翻转 + 一次 paste，不需要任何
模型调用。四个方向共用同一骨架——「取源半区 → 沿垂直于对称轴的方向
翻转 → 贴到目标半区」：

- ``left``   左半镜像后覆盖右半（对称轴为竖直中轴，缺省方向）；
- ``right``  右半镜像后覆盖左半；
- ``top``    上半镜像后覆盖下半（对称轴为水平中轴）；
- ``bottom`` 下半镜像后覆盖上半。

奇数宽/高时中列/中行恰好落在对称轴上保持不变（数学上正确的镜像对称）。
"""

from __future__ import annotations

from PIL import Image

_DIRECTIONS = ("left", "right", "top", "bottom")


def mirror_image(image: Image.Image, direction: str) -> Image.Image:
    """按方向做半区镜像覆盖：源半区翻转后贴到对称的目标半区。

    不认识的方向抛 ValueError（调用方契约：方向只能来自触发层解析，
    不接受自由文本）。源图不被修改；宽/高不足 2 像素时无可覆盖的
    另一半，原样返回副本。
    """
    if direction not in _DIRECTIONS:
        raise ValueError(f"unknown mirror direction: {direction!r}")

    width, height = image.size
    result = image.copy()

    if direction in ("left", "right"):
        if width < 2:
            return result
        mid = (width + 1) // 2
        if direction == "left":
            part = image.crop((0, 0, mid, height))
            result.paste(part.transpose(Image.FLIP_LEFT_RIGHT), (width - mid, 0))
        else:
            part = image.crop((width - mid, 0, width, height))
            result.paste(part.transpose(Image.FLIP_LEFT_RIGHT), (0, 0))
        return result

    # top / bottom：水平中轴
    if height < 2:
        return result
    mid = (height + 1) // 2
    if direction == "top":
        part = image.crop((0, 0, width, mid))
        result.paste(part.transpose(Image.FLIP_TOP_BOTTOM), (0, height - mid))
    else:
        part = image.crop((0, height - mid, width, height))
        result.paste(part.transpose(Image.FLIP_TOP_BOTTOM), (0, 0))
    return result


def mirror_left_to_right(image: Image.Image) -> Image.Image:
    """以竖直中轴为对称轴，把左半部分镜像后覆盖右半部分（缺省方向）。"""
    return mirror_image(image, "left")
