import asyncio
import logging
import os
import shutil
import time
import json
from pathlib import Path
from typing import Dict, Optional, Callable, List, Set
import config

logger = logging.getLogger(__name__)

class StreamSession:
    def __init__(
        self,
        channel_name: str,
        video_path: Path,
        rtmp_url: str,
        loop: bool = True,
        notify_callback: Optional[Callable[[str], None]] = None
    ):
        self.channel_name = channel_name
        self.video_path = video_path
        self.rtmp_url = rtmp_url
        self.loop = loop
        self.notify_callback = notify_callback
        
        self.process: Optional[asyncio.subprocess.Process] = None
        self.monitor_task: Optional[asyncio.Task] = None
        self.start_time: float = 0.0
        self.status: str = "IDLE"  # IDLE, STARTING, LIVE, RECONNECTING, STOPPED, ERROR
        self.reconnect_count: int = 0
        self.is_manually_stopped: bool = False
        self.codec_mode: str = "auto"
        self.error_message: Optional[str] = None

    @property
    def uptime_str(self) -> str:
        if self.status != "LIVE" or self.start_time == 0:
            return "0s"
        elapsed = int(time.time() - self.start_time)
        hours, rem = divmod(elapsed, 3600)
        minutes, seconds = divmod(rem, 60)
        if hours > 0:
            return f"{hours}h {minutes}m {seconds}s"
        elif minutes > 0:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"

class StreamManager:
    """Manages active FFmpeg streaming processes to YouTube, Facebook, etc."""

    def __init__(self):
        self.sessions: Dict[str, StreamSession] = {}

    def get_active_video_paths(self) -> Set[str]:
        """Returns paths of video files currently in use by active streams."""
        return {
            str(s.video_path)
            for s in self.sessions.values()
            if s.status in ("STARTING", "LIVE", "RECONNECTING")
        }

    async def probe_video_info(self, video_path: Path) -> dict:
        """Probes video using ffprobe to detect resolution, codecs, and stream properties."""
        ffprobe_bin = shutil.which("ffprobe")
        default_info = {
            "vcodec": "unknown",
            "acodec": "unknown",
            "width": 1920,
            "height": 1080,
            "fps": 30.0,
            "has_audio": True,
            "is_aac": True
        }
        if not ffprobe_bin:
            return default_info

        try:
            cmd = [
                ffprobe_bin,
                "-v", "error",
                "-show_streams",
                "-of", "json",
                str(video_path)
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            out, _ = await proc.communicate()
            data = json.loads(out.decode("utf-8", errors="ignore"))
            streams = data.get("streams", [])
            v_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
            a_stream = next((s for s in streams if s.get("codec_type") == "audio"), {})

            vcodec = v_stream.get("codec_name", "unknown")
            acodec = a_stream.get("codec_name", "unknown")
            width = int(v_stream.get("width", 0) or 0)
            height = int(v_stream.get("height", 0) or 0)
            has_audio = bool(a_stream)
            is_aac = acodec.lower() in ("aac", "mp3")

            fps = 30.0
            r_fps = v_stream.get("r_frame_rate", "30/1")
            if "/" in r_fps:
                num, den = r_fps.split("/")
                if float(den) > 0:
                    fps = round(float(num) / float(den), 2)
            elif r_fps:
                fps = round(float(r_fps), 2)

            return {
                "vcodec": vcodec,
                "acodec": acodec,
                "width": width,
                "height": height,
                "fps": fps,
                "has_audio": has_audio,
                "is_aac": is_aac
            }
        except Exception as e:
            logger.warning(f"Error probing video {video_path}: {e}")
            return default_info

    def build_ffmpeg_cmd(self, session: StreamSession, video_info: dict, quality_mode: str = "1080p") -> List[str]:
        """Builds FFmpeg argument list tailored for RTMP live streaming with strict 2-second keyframes."""
        ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
        cmd = [ffmpeg_bin, "-re"]

        # Loop indefinitely if enabled
        if session.loop:
            cmd.extend(["-stream_loop", "-1"])

        # Input file
        cmd.extend(["-i", str(session.video_path)])

        vcodec = video_info.get("vcodec", "unknown")
        acodec = video_info.get("acodec", "unknown")
        width = int(video_info.get("width", 0))
        height = int(video_info.get("height", 0))
        has_audio = video_info.get("has_audio", True)
        is_aac = video_info.get("is_aac", True)

        if quality_mode == "copy":
            # Direct stream-copy: 0% CPU, but keyframes depend on the input file
            cmd.extend(["-c:v", "copy"])
            if has_audio:
                if is_aac:
                    cmd.extend(["-c:a", "copy"])
                else:
                    cmd.extend(["-c:a", "aac", "-b:a", "128k", "-ar", "44100"])
            else:
                cmd.extend(["-c:a", "aac"])
            session.codec_mode = "Stream-Copy (Direct pass)"

        elif quality_mode == "720p":
            # 720p HD mode with strict 2-second keyframe cadence (GOP = 60 @ 30fps)
            cmd.extend([
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-tune", "zerolatency",
                "-vf", "scale=-2:720",
                "-r", "30",
                "-b:v", "2800k",
                "-maxrate", "3000k",
                "-bufsize", "6000k",
                "-pix_fmt", "yuv420p",
                "-g", "60",
                "-keyint_min", "60",
                "-sc_threshold", "0",
            ])
            if has_audio:
                if is_aac:
                    cmd.extend(["-c:a", "copy"])
                else:
                    cmd.extend(["-c:a", "aac", "-b:a", "128k", "-ar", "44100"])
            else:
                cmd.extend(["-c:a", "aac"])
            session.codec_mode = "720p HD (2800k, 2s GOP)"

        else:
            # 1080p Full HD mode (Default): Full resolution with strict 2-second keyframes (Prevents Facebook 360p fallback)
            scale_args = []
            if height > 1080 or width > 1920:
                scale_args = ["-vf", "scale=-2:1080"]

            bitrate = "4800k" if (height >= 1080 or width >= 1920 or height == 0) else "3000k"
            maxrate = "5000k" if bitrate == "4800k" else "3200k"
            bufsize = "10000k" if bitrate == "4800k" else "6400k"
            mode_name = "1080p Full HD" if bitrate == "4800k" else f"{height}p HD"

            cmd.extend([
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-tune", "zerolatency",
                *scale_args,
                "-r", "30",
                "-b:v", bitrate,
                "-maxrate", maxrate,
                "-bufsize", bufsize,
                "-pix_fmt", "yuv420p",
                "-g", "60",
                "-keyint_min", "60",
                "-sc_threshold", "0",
            ])
            if has_audio:
                if is_aac:
                    cmd.extend(["-c:a", "copy"])
                else:
                    cmd.extend(["-c:a", "aac", "-b:a", "128k", "-ar", "44100"])
            else:
                cmd.extend(["-c:a", "aac"])
            session.codec_mode = f"{mode_name} ({bitrate}, 2s GOP)"

        # RTMP FLV output
        cmd.extend([
            "-flvflags", "no_duration_filesize",
            "-f", "flv",
            session.rtmp_url
        ])

        return cmd

    async def _launch_process(self, session: StreamSession) -> bool:
        """Launches the FFmpeg streaming subprocess."""
        from settings_manager import settings_manager
        video_info = await self.probe_video_info(session.video_path)
        quality_mode = settings_manager.get_quality()
        cmd = self.build_ffmpeg_cmd(session, video_info=video_info, quality_mode=quality_mode)
        
        log_file_path = config.LOGS_DIR / f"{session.channel_name}_ffmpeg.log"
        logger.info(f"Starting FFmpeg for channel [{session.channel_name}]: {' '.join(cmd[:6])} ...")

        try:
            log_file = open(log_file_path, "wb")
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT
            )
            session.process = process
            session.start_time = time.time()
            session.status = "LIVE"
            return True
        except Exception as e:
            session.status = "ERROR"
            session.error_message = str(e)
            logger.error(f"Failed to start FFmpeg for {session.channel_name}: {e}")
            return False

    async def _monitor_stream(self, session: StreamSession):
        """Watchdog loop: monitors FFmpeg process and auto-reconnects if disconnected."""
        while not session.is_manually_stopped:
            if session.process:
                return_code = await session.process.wait()
                logger.warning(f"FFmpeg process for channel [{session.channel_name}] exited with code {return_code}")

                if session.is_manually_stopped:
                    session.status = "STOPPED"
                    break

                # Auto-recovery logic
                if config.AUTO_RECONNECT and session.reconnect_count < config.MAX_RECONNECT_ATTEMPTS:
                    session.reconnect_count += 1
                    session.status = "RECONNECTING"
                    msg = (
                        f"⚠️ Live stream for **{session.channel_name}** was interrupted (code {return_code}).\n"
                        f"🔄 Auto-reconnecting attempt {session.reconnect_count}/{config.MAX_RECONNECT_ATTEMPTS} in {config.RECONNECT_DELAY_SECONDS}s..."
                    )
                    logger.info(msg)
                    if session.notify_callback:
                        asyncio.create_task(session.notify_callback(msg))

                    await asyncio.sleep(config.RECONNECT_DELAY_SECONDS)
                    success = await self._launch_process(session)
                    if success:
                        succ_msg = f"✅ Stream for **{session.channel_name}** successfully reconnected!"
                        logger.info(succ_msg)
                        if session.notify_callback:
                            asyncio.create_task(session.notify_callback(succ_msg))
                    else:
                        continue
                else:
                    session.status = "ERROR"
                    err_msg = f"❌ Stream for **{session.channel_name}** stopped after reaching max reconnect attempts."
                    if session.notify_callback:
                        asyncio.create_task(session.notify_callback(err_msg))
                    break
            else:
                break

    async def start_stream(
        self,
        channel_name: str,
        video_path: Path,
        rtmp_url: str,
        loop: bool = True,
        notify_callback: Optional[Callable[[str], None]] = None
    ) -> bool:
        """Starts a live stream on a channel."""
        # Stop any existing stream on this channel first
        if channel_name in self.sessions:
            await self.stop_stream(channel_name)

        session = StreamSession(
            channel_name=channel_name,
            video_path=video_path,
            rtmp_url=rtmp_url,
            loop=loop,
            notify_callback=notify_callback
        )
        session.status = "STARTING"
        self.sessions[channel_name] = session

        success = await self._launch_process(session)
        if success:
            session.monitor_task = asyncio.create_task(self._monitor_stream(session))
            return True
        return False

    async def stop_stream(self, channel_name: str) -> bool:
        """Gracefully stops an active stream."""
        session = self.sessions.get(channel_name)
        if not session:
            return False

        session.is_manually_stopped = True
        session.status = "STOPPED"

        if session.process and session.process.returncode is None:
            try:
                session.process.terminate()
                try:
                    await asyncio.wait_for(session.process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    session.process.kill()
            except Exception as e:
                logger.error(f"Error terminating stream {channel_name}: {e}")

        if session.monitor_task and not session.monitor_task.done():
            session.monitor_task.cancel()

        del self.sessions[channel_name]
        return True

    async def restart_stream(self, channel_name: str) -> bool:
        """Restarts an active stream from 0:00 without dropping destination settings."""
        session = self.sessions.get(channel_name)
        if not session:
            return False

        video_path = session.video_path
        rtmp_url = session.rtmp_url
        loop = session.loop
        notify_callback = session.notify_callback

        await self.stop_stream(channel_name)
        await asyncio.sleep(0.5)

        return await self.start_stream(
            channel_name=channel_name,
            video_path=video_path,
            rtmp_url=rtmp_url,
            loop=loop,
            notify_callback=notify_callback
        )

    async def stop_all_streams(self) -> int:
        """Stops all running streams. Returns count of stopped streams."""
        channel_names = list(self.sessions.keys())
        for ch in channel_names:
            await self.stop_stream(ch)
        return len(channel_names)

    def get_session(self, channel_name: str) -> Optional[StreamSession]:
        return self.sessions.get(channel_name)

    def list_active(self) -> Dict[str, StreamSession]:
        return self.sessions

# Global StreamManager instance
stream_manager = StreamManager()
