import os
import re
import time
import hashlib
import logging
from pathlib import Path
from typing import Optional, Callable, Set
import aiohttp
import psutil
import asyncio
import inspect
import urllib.parse
import config

logger = logging.getLogger(__name__)

def is_social_media_url(url: str) -> bool:
    """Checks if the URL is from YouTube, Facebook, TikTok, Instagram, etc."""
    try:
        domain = urllib.parse.urlparse(url).netloc.lower()
        social_domains = (
            "youtube.com", "youtu.be",
            "facebook.com", "fb.watch", "fb.com",
            "instagram.com",
            "tiktok.com",
            "twitter.com", "x.com",
            "vimeo.com", "dailymotion.com",
            "twitch.tv"
        )
        return any(d in domain for d in social_domains)
    except Exception:
        return False

def encode_url_safely(url: str) -> str:
    """Safely percent-encodes Unicode characters in URL paths and query strings for HTTP requests."""
    try:
        parsed = urllib.parse.urlsplit(url)
        encoded_path = urllib.parse.quote(urllib.parse.unquote(parsed.path), safe="/:@&+$,~")
        encoded_query = urllib.parse.quote(urllib.parse.unquote(parsed.query), safe="=&+$,~")
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, encoded_path, encoded_query, parsed.fragment))
    except Exception:
        return url

def sanitize_filename(name: str) -> str:
    """Removes filesystem-unsafe characters while preserving Unicode characters."""
    cleaned = re.sub(r'[\\/*?:"<>|\r\n\t]', '_', name).strip()
    return cleaned

def get_url_cache_path(url: str, content_disposition: Optional[str] = None) -> Path:
    """Generates a stable local file path for a URL or returns existing cached path."""
    unquoted = urllib.parse.unquote(url)
    url_hash = hashlib.sha256(unquoted.encode("utf-8")).hexdigest()[:12]

    # Check if an existing file with this hash prefix already exists
    if config.DOWNLOAD_DIR.exists():
        for p in config.DOWNLOAD_DIR.glob(f"{url_hash}_*"):
            if p.is_file() and not p.name.endswith(".downloading") and not p.name.endswith(".part") and p.stat().st_size > 0:
                return p

    # Try to extract filename from URL path
    clean_url = unquoted.split("?")[0].rstrip("/")
    original_name = clean_url.split("/")[-1]

    if not original_name or "." not in original_name:
        original_name = f"video_{url_hash}.mp4"
    else:
        original_name = sanitize_filename(original_name)
        if len(original_name.encode("utf-8")) > 100:
            ext = original_name.split('.')[-1] if '.' in original_name else 'mp4'
            original_name = f"video_{url_hash}.{ext}"
        # Prepend short hash to avoid collisions
        original_name = f"{url_hash}_{original_name}"

    return config.DOWNLOAD_DIR / original_name

async def _notify_helper(cb, dl: int, total: int, **kwargs):
    try:
        if inspect.iscoroutinefunction(cb):
            sig = inspect.signature(cb)
            if len(sig.parameters) > 2 or any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
                await cb(dl, total, **kwargs)
            else:
                await cb(dl, total)
        else:
            sig = inspect.signature(cb)
            if len(sig.parameters) > 2 or any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
                res = cb(dl, total, **kwargs)
            else:
                res = cb(dl, total)
            if inspect.isawaitable(res):
                await res
    except Exception as e:
        logger.debug(f"Progress notification error: {e}")

def _sync_ytdlp_download(url: str, url_hash: str, loop, progress_callback) -> Path:
    import yt_dlp
    outtmpl = str(config.DOWNLOAD_DIR / f"{url_hash}_%(title).50s.%(ext)s")

    last_time = [0.0]

    def hook(d):
        if not progress_callback or not loop or not loop.is_running():
            return
        status = d.get("status")
        if status == "downloading":
            now = time.time()
            if (now - last_time[0]) < 0.5:
                return
            last_time[0] = now
            dl = d.get("downloaded_bytes", 0)
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            speed = d.get("speed") or 0.0
            eta = d.get("eta") or 0
            fn = d.get("filename", "")
            asyncio.run_coroutine_threadsafe(
                _notify_helper(progress_callback, dl, total, speed=speed, eta=eta, filename=fn),
                loop
            )
        elif status == "finished":
            total = d.get("total_bytes") or d.get("downloaded_bytes", 0)
            fn = d.get("filename", "")
            asyncio.run_coroutine_threadsafe(
                _notify_helper(progress_callback, total, total, speed=0.0, eta=0, filename=fn),
                loop
            )

    ydl_opts = {
        "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "merge_output_format": "mp4",
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        # Search for created file with url_hash prefix
        for f in config.DOWNLOAD_DIR.glob(f"{url_hash}_*"):
            if f.is_file() and not f.name.endswith(".part") and not f.name.endswith(".downloading") and f.stat().st_size > 0:
                return f
        fn = ydl.prepare_filename(info)
        p = Path(fn)
        if p.exists() and p.stat().st_size > 0:
            return p
        mp4_p = p.with_suffix(".mp4")
        if mp4_p.exists() and mp4_p.stat().st_size > 0:
            return mp4_p
        raise RuntimeError("yt-dlp finished but output video file was not found.")

async def download_with_ytdlp(
    url: str,
    progress_callback: Optional[Callable[[int, int], None]] = None
) -> Path:
    cached = get_url_cache_path(url)
    if cached.exists() and cached.stat().st_size > 0:
        logger.info(f"Video already cached: {cached}")
        return cached

    unquoted = urllib.parse.unquote(url)
    url_hash = hashlib.sha256(unquoted.encode("utf-8")).hexdigest()[:12]
    loop = asyncio.get_running_loop()

    logger.info(f"Starting yt-dlp download for {url}")
    return await asyncio.to_thread(_sync_ytdlp_download, url, url_hash, loop, progress_callback)

async def download_video(
    url: str,
    progress_callback: Optional[Callable[[int, int], None]] = None
) -> Path:
    """
    Downloads a video from a direct HTTP/HTTPS URL or platform link (YouTube, Facebook, etc.).
    Uses chunked streaming for direct URLs and yt-dlp for media platform links.
    """
    # 1. Social media & video platforms (YouTube, Facebook, Instagram, TikTok, etc.)
    if is_social_media_url(url):
        return await download_with_ytdlp(url, progress_callback)

    # 2. Check if cached file already exists
    target_path = get_url_cache_path(url)
    if target_path.exists() and target_path.stat().st_size > 0:
        logger.info(f"Video already cached: {target_path}")
        return target_path

    # 3. Direct HTTP/HTTPS download
    temp_path = target_path.with_suffix(".downloading")
    safe_url = encode_url_safely(url)
    logger.info(f"Starting download from {safe_url} to {temp_path}")

    timeout = aiohttp.ClientTimeout(total=3600)  # 1 hour max download
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(safe_url, allow_redirects=True) as response:
                content_type = response.headers.get("Content-Type", "").lower()
                
                # If server returns HTML instead of video or stream, fallback to yt-dlp extractor
                if "text/html" in content_type:
                    logger.info(f"URL returned HTML, falling back to yt-dlp: {url}")
                    return await download_with_ytdlp(url, progress_callback)

                if response.status not in (200, 206):
                    raise RuntimeError(f"Failed to download video: HTTP status {response.status}")

                total_size = int(response.headers.get("Content-Length", 0))
                downloaded = 0
                chunk_size = 512 * 1024  # 512 KB chunks
                dl_start_time = time.time()
                last_cb_time = [0.0]

                await _notify_helper(progress_callback, 0, total_size, speed=0.0, eta=0)

                with open(temp_path, "wb") as f:
                    async for chunk in response.content.iter_chunked(chunk_size):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            now = time.time()
                            if (now - last_cb_time[0]) >= 0.5 or (total_size > 0 and downloaded >= total_size):
                                last_cb_time[0] = now
                                elapsed = max(0.001, now - dl_start_time)
                                speed = downloaded / elapsed
                                eta = int((total_size - downloaded) / speed) if (speed > 0 and total_size > downloaded) else 0
                                await _notify_helper(progress_callback, downloaded, total_size, speed=speed, eta=eta)

        # Rename temp file to target once complete
        if temp_path.exists():
            temp_path.rename(target_path)

        final_size = target_path.stat().st_size
        await _notify_helper(progress_callback, final_size, final_size if total_size == 0 else total_size, speed=0.0, eta=0)

        logger.info(f"Download complete: {target_path} ({final_size} bytes)")
        return target_path

    except Exception as e:
        logger.warning(f"Direct download failed for {url}: {e}. Attempting yt-dlp fallback...")
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass
        try:
            return await download_with_ytdlp(url, progress_callback)
        except Exception as yt_err:
            logger.error(f"yt-dlp fallback also failed: {yt_err}")
            raise e

def cleanup_disk_cache(active_paths: Set[str]):
    """
    Cleans up old, unused video files from downloads folder if disk space is low.
    Never deletes videos that are currently being streamed.
    """
    try:
        disk = psutil.disk_usage(str(config.DATA_DIR))
        percent_free = (disk.free / disk.total) * 100

        logger.info(f"Disk check: {percent_free:.1f}% free (Threshold: {config.DISK_MIN_FREE_PERCENT}%)")

        if percent_free < config.DISK_MIN_FREE_PERCENT:
            logger.warning(f"Disk space low ({percent_free:.1f}%). Cleaning up inactive downloads...")
            
            # List all video files sorted by oldest access/modification time
            video_files = sorted(
                list(config.DOWNLOAD_DIR.glob("*")),
                key=lambda p: p.stat().st_mtime
            )

            for p in video_files:
                if str(p) not in active_paths and not p.name.endswith(".downloading"):
                    logger.info(f"Deleting cached video to free space: {p}")
                    try:
                        p.unlink()
                    except Exception as err:
                        logger.error(f"Failed to delete {p}: {err}")

                # Re-check disk space
                disk = psutil.disk_usage(str(config.DATA_DIR))
                if ((disk.free / disk.total) * 100) >= config.DISK_MIN_FREE_PERCENT:
                    break
    except Exception as e:
        logger.error(f"Error during disk cache cleanup: {e}")
