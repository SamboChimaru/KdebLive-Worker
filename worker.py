import asyncio
import hmac
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

from aiohttp import web
import psutil

import config
from streamer import stream_manager
from downloader import download_video
from file_manager import file_manager
from channel_manager import channel_manager
from settings_manager import settings_manager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] WorkerNode: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("WorkerNode")

WORKER_START_TIME = time.time()

# --- SECURITY MIDDLEWARE ---

@web.middleware
async def auth_middleware(request: web.Request, handler):
    # Allow public access to root ping and health check
    if request.path in ("/", "/health", "/api/ping", "/api/health"):
        return await handler(request)

    # Check expected API key
    expected_key = getattr(config, "WORKER_API_KEY", "").strip()
    if not expected_key or expected_key.lower() in ("none", "disabled", "false"):
        return await handler(request)

    # Validate API Key
    api_key = request.headers.get("X-API-Key", "").strip()
    if not api_key:
        auth_header = request.headers.get("Authorization", "").strip()
        if auth_header.startswith("Bearer "):
            api_key = auth_header[7:].strip()

    # Compare key: accept exact match, or either default key if default is configured
    is_valid = False
    if api_key:
        if hmac.compare_digest(api_key, expected_key):
            is_valid = True
        elif expected_key in ("default_secret_key", "default_worker_secret") and api_key in ("default_secret_key", "default_worker_secret"):
            is_valid = True

    if not is_valid:
        return web.json_response(
            {
                "error": "Unauthorized",
                "message": "Invalid or missing X-API-Key header. Ensure your controller's API key matches WORKER_API_KEY on this worker node."
            },
            status=401
        )

    return await handler(request)


# --- ROUTE HANDLERS ---

async def handle_ping(request: web.Request) -> web.Response:
    """Lightweight health ping."""
    return web.json_response({
        "status": "online",
        "service": "24/7 Live Stream Worker Node",
        "version": "2.0",
        "uptime_seconds": int(time.time() - WORKER_START_TIME)
    })

async def handle_health(request: web.Request) -> web.Response:
    """Returns real-time host hardware & streaming metrics."""
    cpu_percent = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(str(config.DATA_DIR))
    net = psutil.net_io_counters()

    active_streams = stream_manager.list_active()
    active_count = len([
        s for s in active_streams.values()
        if (s.is_running() if hasattr(s, "is_running") else s.status in ("LIVE", "STARTING", "RECONNECTING"))
    ])

    return web.json_response({
        "status": "online",
        "timestamp": datetime.now().isoformat(),
        "uptime_seconds": int(time.time() - WORKER_START_TIME),
        "hardware": {
            "cpu_percent": cpu_percent,
            "cpu_cores": psutil.cpu_count(logical=True),
            "ram_used_mb": mem.used // (1024 * 1024),
            "ram_total_mb": mem.total // (1024 * 1024),
            "ram_percent": mem.percent,
            "disk_free_gb": round(disk.free / (1024 ** 3), 2),
            "disk_total_gb": round(disk.total / (1024 ** 3), 2),
            "disk_percent": disk.percent,
            "net_sent_gb": round(net.bytes_sent / (1024 ** 3), 2),
            "net_recv_gb": round(net.bytes_recv / (1024 ** 3), 2),
        },
        "streaming": {
            "active_streams_count": active_count,
            "total_sessions": active_count
        }
    })

async def handle_list_streams(request: web.Request) -> web.Response:
    """Lists all streaming sessions on this worker node with full Watchdog telemetry."""
    sessions_data = {}
    for name, session in stream_manager.list_active().items():
        if (hasattr(session, "is_running") and session.is_running()) or session.status in ("LIVE", "STARTING", "RECONNECTING"):
            info = stream_manager.get_session_info(name)
            if info:
                sessions_data[name] = info
    return web.json_response({"streams": sessions_data})

async def handle_start_stream(request: web.Request) -> web.Response:
    """Starts FFmpeg live broadcast on this worker node (Single Video or Playlist)."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Malformed JSON body"}, status=400)

    channel_name = data.get("channel_name", "").strip().lower()
    if not channel_name:
        return web.json_response({"error": "channel_name is required"}, status=400)

    # 1. Resolve video file path or playlist items
    target_path: Optional[Path] = None
    playlist_files: List[Path] = []
    playlist_names: List[str] = []

    playlist_raw = data.get("playlist_ids") or data.get("playlist")
    if playlist_raw and isinstance(playlist_raw, list):
        for item in playlist_raw:
            p_file = None
            if isinstance(item, str):
                p_file = file_manager.get_file(item)
                if not p_file:
                    cand = Path(item)
                    if cand.exists():
                        p_file = cand
                    elif (config.DOWNLOAD_DIR / cand.name).exists():
                        p_file = config.DOWNLOAD_DIR / cand.name
            if p_file and p_file.exists():
                playlist_files.append(p_file)
                playlist_names.append(p_file.name)

        if not playlist_files:
            return web.json_response({"error": "None of the playlist videos were found on worker node"}, status=404)
        target_path = playlist_files[0]
    else:
        file_id = data.get("file_id")
        video_path_str = data.get("video_path")
        video_url = data.get("video_url")

        if file_id:
            target_path = file_manager.get_file(file_id)
        elif video_path_str:
            candidate = Path(video_path_str)
            if candidate.is_absolute():
                target_path = candidate
            else:
                target_path = config.DOWNLOAD_DIR / candidate

        # If URL provided and no local file, download on this worker node
        if not target_path or not target_path.exists():
            if video_url:
                logger.info(f"Downloading stream source video from {video_url}...")
                try:
                    target_path = await download_video(video_url)
                except Exception as e:
                    return web.json_response({"error": f"Failed to download video: {str(e)}"}, status=500)
            else:
                return web.json_response({"error": "Video file not found and no video_url provided"}, status=404)

        if not target_path or not target_path.exists():
            return web.json_response({"error": "Video file not found on worker node"}, status=404)

    # 2. Resolve RTMP URL
    rtmp_url = data.get("rtmp_url")
    if not rtmp_url:
        rtmp_url = channel_manager.get_rtmp_url(channel_name)
    if not rtmp_url:
        return web.json_response({"error": f"Channel [{channel_name}] not found and no rtmp_url provided"}, status=400)

    loop_setting = data.get("loop", config.DEFAULT_LOOP)
    quality_mode = data.get("quality", settings_manager.get_quality())
    if quality_mode:
        settings_manager.set_quality(quality_mode)

    mode_label = f"Playlist ({len(playlist_files)} videos)" if playlist_files else target_path.name
    logger.info(f"Starting live stream: [{channel_name}] -> {mode_label} (Loop: {loop_setting}, Quality: {quality_mode})")

    started = await stream_manager.start_stream(
        channel_name=channel_name,
        video_path=target_path,
        rtmp_url=rtmp_url,
        loop=loop_setting,
        playlist_files=playlist_files if playlist_files else None,
        playlist_names=playlist_names if playlist_names else None
    )

    if started:
        session = stream_manager.get_session(channel_name)
        file_size_str = ""
        if target_path and target_path.exists():
            try:
                sz_mb = target_path.stat().st_size / (1024 * 1024)
                file_size_str = f"{sz_mb / 1024:.2f} GB" if sz_mb >= 1024 else f"{sz_mb:.1f} MB"
            except Exception:
                pass

        return web.json_response({
            "status": "success",
            "channel": channel_name,
            "stream_status": session.status if session else "STARTING",
            "is_playlist": bool(playlist_files),
            "playlist_count": len(playlist_files),
            "now_playing": session.now_playing if session else target_path.name,
            "video_name": target_path.name,
            "file_size_str": file_size_str
        })
    else:
        return web.json_response({"error": "Failed to start FFmpeg process"}, status=500)

async def handle_stop_stream(request: web.Request) -> web.Response:
    """Stops one or all streams."""
    try:
        data = await request.json()
    except Exception:
        data = {}

    channel_name = data.get("channel_name", "").strip().lower()
    if not channel_name or channel_name == "all":
        count = await stream_manager.stop_all_streams()
        return web.json_response({"status": "success", "stopped_count": count})

    stopped = await stream_manager.stop_stream(channel_name)
    if stopped:
        return web.json_response({"status": "success", "channel": channel_name, "stopped": True})
    return web.json_response({"error": f"No active stream for [{channel_name}]"}, status=404)

async def handle_restart_stream(request: web.Request) -> web.Response:
    """Restarts an active stream from 00:00:00 (Live Sync)."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Malformed JSON body"}, status=400)

    channel_name = data.get("channel_name", "").strip().lower()
    if not channel_name:
        return web.json_response({"error": "channel_name is required"}, status=400)

    restarted = await stream_manager.restart_stream(channel_name)
    if restarted:
        return web.json_response({"status": "success", "channel": channel_name, "restarted": True})
    return web.json_response({"error": f"Could not restart channel [{channel_name}]"}, status=400)

async def handle_skip_stream(request: web.Request) -> web.Response:
    """Skips to the next video in an active playlist with seamless transition."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Malformed JSON body"}, status=400)

    channel_name = data.get("channel_name", "").strip().lower()
    if not channel_name:
        return web.json_response({"error": "channel_name is required"}, status=400)

    success, msg = await stream_manager.skip_next(channel_name)
    if success:
        return web.json_response({"status": "success", "channel": channel_name, "message": msg})
    return web.json_response({"error": msg}, status=400)

# --- FILE MANAGEMENT ROUTES ---

async def handle_list_files(request: web.Request) -> web.Response:
    """Lists saved videos and storage metrics."""
    files = file_manager.list_files()
    storage = file_manager.get_storage_stats()
    return web.json_response({"files": files, "storage": storage})

async def handle_download_file(request: web.Request) -> web.Response:
    """Downloads a video file onto the worker node."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Malformed JSON body"}, status=400)

    url = data.get("url", "").strip()
    if not url:
        return web.json_response({"error": "url is required"}, status=400)

    try:
        downloaded_path = await download_video(url)
        return web.json_response({
            "status": "success",
            "filename": downloaded_path.name,
            "size_bytes": downloaded_path.stat().st_size
        })
    except Exception as e:
        logger.error(f"Download failed for {url}: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def handle_delete_file(request: web.Request) -> web.Response:
    """Deletes a saved video file by file_id."""
    file_id = request.match_info.get("file_id")
    if not file_id:
        return web.json_response({"error": "file_id required"}, status=400)

    success, msg = file_manager.delete_file(file_id)
    if success:
        return web.json_response({"status": "success", "deleted": True})
    return web.json_response({"error": msg or "File not found or currently streaming"}, status=400)

async def handle_clean_files(request: web.Request) -> web.Response:
    """Purges all inactive video files from storage."""
    count, freed_mb = file_manager.delete_all_inactive()
    return web.json_response({
        "status": "success",
        "deleted_count": count,
        "freed_mb": round(freed_mb, 2)
    })

# --- CHANNEL ROUTES ---

async def handle_list_channels(request: web.Request) -> web.Response:
    """Lists channels configured on this worker node."""
    channels = channel_manager.list_channels()
    # Mask stream keys for security
    masked = {}
    for k, v in channels.items():
        sk = v.get("stream_key", "")
        masked_key = sk[:4] + "••••" + sk[-4:] if len(sk) > 8 else "••••"
        masked[k] = {"name": v.get("name", k), "platform": v.get("platform", "live"), "stream_key": masked_key}
    return web.json_response({"channels": masked})

async def handle_add_channel(request: web.Request) -> web.Response:
    """Adds or updates a channel stream key on this worker node."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Malformed JSON body"}, status=400)

    name = data.get("name", "").strip().lower()
    platform = data.get("platform", "youtube").strip().lower()
    stream_key = data.get("stream_key", "").strip()

    if not name or not stream_key:
        return web.json_response({"error": "name and stream_key are required"}, status=400)

    channel_manager.add_channel(name, platform, stream_key)
    return web.json_response({"status": "success", "channel": name, "platform": platform})

async def handle_delete_channel(request: web.Request) -> web.Response:
    """Removes a channel configuration."""
    name = request.match_info.get("name", "").strip().lower()
    if channel_manager.remove_channel(name):
        return web.json_response({"status": "success", "channel": name})
    return web.json_response({"error": f"Channel [{name}] not found"}, status=404)


# --- APPLICATION SETUP ---

def create_app() -> web.Application:
    app = web.Application(middlewares=[auth_middleware])

    # Health & System
    app.router.add_get("/", handle_ping)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/health", handle_health)

    # Streams
    app.router.add_get("/api/streams", handle_list_streams)
    app.router.add_post("/api/streams/start", handle_start_stream)
    app.router.add_post("/api/streams/stop", handle_stop_stream)
    app.router.add_post("/api/streams/restart", handle_restart_stream)
    app.router.add_post("/api/streams/skip", handle_skip_stream)

    # Files
    app.router.add_get("/api/files", handle_list_files)
    app.router.add_post("/api/files/download", handle_download_file)
    app.router.add_delete("/api/files/clean", handle_clean_files)
    app.router.add_delete("/api/files/{file_id}", handle_delete_file)

    # Channels
    app.router.add_get("/api/channels", handle_list_channels)
    app.router.add_post("/api/channels", handle_add_channel)
    app.router.add_delete("/api/channels/{name}", handle_delete_channel)

    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)

    return app

async def background_resource_guard(app: web.Application):
    """Background task: monitors host disk/RAM and purges inactive videos if near capacity."""
    logger.info("🛡 Host Resource Guard active (monitoring RAM & Disk thresholds)")
    try:
        while True:
            await asyncio.sleep(30)
            try:
                mem = psutil.virtual_memory()
                disk = psutil.disk_usage(str(config.DATA_DIR))
                disk_max = getattr(config, "DISK_AUTO_CLEAN_PERCENT", 85.0)
                ram_max = getattr(config, "RAM_AUTO_CLEAN_PERCENT", 88.0)

                if disk.percent > disk_max or mem.percent > ram_max:
                    logger.warning(
                        f"⚠️ Resource guard triggered! Disk: {disk.percent}% (Limit: {disk_max}%), "
                        f"RAM: {mem.percent}% (Limit: {ram_max}%). Purging inactive files..."
                    )
                    count, freed = file_manager.delete_all_inactive()
                    logger.info(f"Purged {count} inactive files, freed {freed:.1f} MB to protect active streams.")
            except Exception as e:
                logger.debug(f"Resource guard error: {e}")
    except asyncio.CancelledError:
        pass

async def start_background_tasks(app: web.Application):
    app["resource_guard"] = asyncio.create_task(background_resource_guard(app))

async def cleanup_background_tasks(app: web.Application):
    task = app.get("resource_guard")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

if __name__ == "__main__":
    host = config.WORKER_HOST
    port = config.WORKER_PORT
    logger.info("==========================================================")
    logger.info("  🚀 Starting 24/7 Live Stream Worker Node (Watchdog & Playlist)")
    logger.info(f"  • Host: {host}")
    logger.info(f"  • Port: {port}")
    logger.info(f"  • API Key protection: {'Active' if config.WORKER_API_KEY else 'Disabled (Warning!)'}")
    logger.info("==========================================================")
    app = create_app()
    web.run_app(app, host=host, port=port)
