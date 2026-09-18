"""章节图片打包：ZIP / CBZ / PDF。img2pdf 缺库时 PDF 自动降级为 ZIP。"""

from __future__ import annotations

import zipfile
from pathlib import Path

try:
    import img2pdf
except Exception:  # pragma: no cover - img2pdf 未安装
    img2pdf = None


def pack_zip(image_paths: list[str], output: Path) -> None:
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, path in enumerate(image_paths):
            if not path:
                continue
            ext = Path(path).suffix or ".jpg"
            zf.write(path, f"{i:04d}{ext}")


def pack_cbz(image_paths: list[str], output: Path) -> None:
    pack_zip(image_paths, output)


def pack_pdf(image_paths: list[str], output: Path) -> None:
    if img2pdf is None:
        raise RuntimeError("img2pdf 未安装，无法生成 PDF（pip install img2pdf）")
    valid = [p for p in image_paths if p and Path(p).exists()]
    if not valid:
        raise ValueError("没有可打包的图片")
    with open(output, "wb") as f:
        f.write(img2pdf.convert(valid))


def sanitize_filename(name: str, max_len: int = 50) -> str:
    cleaned = "".join(c for c in str(name) if c not in r'<>:"/\|?*').strip()
    return cleaned[:max_len] or "untitled"


def normalize_pack_format(fmt: str) -> str:
    return fmt if fmt in ("pdf", "cbz", "zip") else "zip"


def build_chapter_output_path(directory: Path, title: str, label: str, fmt: str) -> Path:
    return directory / f"{sanitize_filename(title)}_{sanitize_filename(label)}.{fmt}"


def pack_images(image_paths: list[str], output: Path, fmt: str) -> str:
    """打包并返回实际格式；PDF 依赖缺失时降级为 ZIP。"""
    if fmt == "pdf" and img2pdf is None:
        fmt = "zip"
    if fmt == "pdf":
        pack_pdf(image_paths, output)
    elif fmt == "cbz":
        pack_cbz(image_paths, output)
    else:
        pack_zip(image_paths, output)
    return fmt


def parse_download_args(raw_message: str, default_fmt: str = "pdf") -> tuple[str, str, str]:
    """解析「漫画 下载 <漫画名或ID> <章节号> [格式]」，返回 (漫画, 章节, 格式)。"""
    tokens = raw_message.strip().split()
    try:
        cmd_idx = tokens.index("下载")
        args = tokens[cmd_idx + 1:]
    except ValueError:
        return "", "", default_fmt

    fmt = default_fmt
    if len(args) >= 3 and args[-1].lower() in ("zip", "pdf", "cbz"):
        fmt = args[-1].lower()
        manga = args[0]
        chapter = " ".join(args[1:-1])
    elif len(args) >= 2:
        manga = args[0]
        chapter = " ".join(args[1:])
    else:
        manga = args[0] if args else ""
        chapter = ""
    return manga, chapter, fmt
