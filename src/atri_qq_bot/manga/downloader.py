"""章节图片下载：httpx 并发下载 + 指数退避重试。"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .client import SuwayomiClient

LOGGER = logging.getLogger("atri.manga")

_DOWNLOAD_TIMEOUT = 30.0


async def download_one(
    client: httpx.AsyncClient, url: str, dest: Path, retries: int = 3
) -> bool:
    for attempt in range(retries):
        try:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.content
                ext = ".jpg"
                ct = resp.headers.get("Content-Type", "")
                if "png" in ct:
                    ext = ".png"
                elif "webp" in ct:
                    ext = ".webp"
                dest.with_suffix(ext).write_bytes(data)
                return True
            if resp.status_code < 500:
                return False
            LOGGER.warning(
                "图片下载 HTTP %s，重试 %s/%s: %s",
                resp.status_code, attempt + 1, retries, url,
            )
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            LOGGER.warning(
                "图片下载超时/网络错误，重试 %s/%s: %s",
                attempt + 1, retries, exc,
            )
        except Exception as exc:
            LOGGER.warning("图片下载异常: %s", exc)
            return False
        if attempt < retries - 1:
            await asyncio.sleep(0.5 * (2**attempt))
    return False


async def download_images(
    urls: list[str],
    concurrency: int = 6,
    custom_tmp: str = "",
    retries: int = 3,
    headers: dict[str, str] | None = None,
) -> tuple[list[str], Path]:
    """并发下载全部图片到临时目录，返回 (本地路径列表, 临时目录)；失败位置为空串。"""
    if custom_tmp:
        Path(custom_tmp).mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="suwayomi_", dir=custom_tmp or None))
    try:
        sem = asyncio.Semaphore(max(1, concurrency))

        async def _download(url: str, dest: Path) -> bool:
            async with sem:
                async with httpx.AsyncClient(
                    timeout=_DOWNLOAD_TIMEOUT, headers=headers or {}, follow_redirects=True
                ) as client:
                    return await download_one(client, url, dest, retries)

        results = await asyncio.gather(
            *(_download(url, tmp_dir / f"{i:04d}.jpg") for i, url in enumerate(urls)),
            return_exceptions=True,
        )

        paths: list[str] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                LOGGER.warning("图片 %s 下载异常: %s", i + 1, result)
                paths.append("")
            elif result:
                matches = sorted(tmp_dir.glob(f"{i:04d}.*"))
                paths.append(str(matches[-1]) if matches else "")
            else:
                paths.append("")
        return paths, tmp_dir
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise


def resolve_image_url(
    client: SuwayomiClient,
    thumbnail_url: str | None,
    auth_headers: dict[str, str] | None,
) -> tuple[str | None, dict[str, str] | None]:
    """解析封面 URL。相对路径带认证头；绝对 URL 仅在同源时带认证，避免凭据泄漏。"""
    if not thumbnail_url:
        return None, None

    if thumbnail_url.startswith(("http://", "https://")):
        url = thumbnail_url
        use_headers = None
        if client.server_url:
            server = urlparse(client.server_url)
            target = urlparse(thumbnail_url)
            server_port = server.port or (443 if server.scheme == "https" else 80 if server.scheme == "http" else None)
            target_port = target.port or (443 if target.scheme == "https" else 80 if target.scheme == "http" else None)
            if (
                server.scheme == target.scheme
                and server.hostname == target.hostname
                and server_port == target_port
            ):
                use_headers = auth_headers
    else:
        url = client.build_image_url(thumbnail_url)
        use_headers = auth_headers

    return url, use_headers


async def download_cover(
    client: SuwayomiClient,
    thumbnail_url: str | None,
    custom_tmp: str = "",
    retries: int = 3,
    headers: dict[str, str] | None = None,
) -> tuple[str | None, Path | None]:
    """下载单张封面，返回 (本地路径, 临时目录)；失败返回 (None, None)。"""
    url, use_headers = resolve_image_url(client, thumbnail_url, headers)
    if url is None:
        return None, None

    try:
        paths, tmp_dir = await download_images(
            [url], concurrency=1, custom_tmp=custom_tmp, retries=retries, headers=use_headers
        )
    except (httpx.HTTPError, OSError) as exc:
        LOGGER.warning("封面下载失败: %s", exc)
        return None, None

    if not paths or not paths[0]:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None, None
    return paths[0], tmp_dir


async def fetch_pages_local(
    client: SuwayomiClient,
    chapter_id: int,
    max_pages: int = 0,
    concurrency: int = 6,
    custom_tmp: str = "",
    retries: int = 3,
    headers: dict[str, str] | None = None,
) -> tuple[int, list[str], list[str], Path | None]:
    """获取章节页并下载。返回 (总页数, 页URL列表, 本地路径列表, 临时目录)。"""
    pages = await client.fetch_chapter_pages(chapter_id)
    if not pages:
        return 0, [], [], None
    total_pages = len(pages)
    if max_pages > 0:
        pages = pages[:max_pages]
    page_urls = [client.build_image_url(p) for p in pages]
    local_paths, tmp_dir = await download_images(
        page_urls, concurrency, custom_tmp, retries, headers
    )
    return total_pages, page_urls, local_paths, tmp_dir


def schedule_cleanup(tmp_dir: Path | None, delay: int = 60) -> asyncio.Task | None:
    """延迟清理临时目录；打包/发送完成后调用。"""
    if tmp_dir is None:
        return None

    async def _cleanup():
        await asyncio.sleep(delay)
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: shutil.rmtree(tmp_dir, ignore_errors=True)
            )
        except Exception:
            pass

    try:
        task = asyncio.create_task(_cleanup())
    except RuntimeError:  # 无事件循环（测试/工具环境）直接同步清理
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None
    return task
