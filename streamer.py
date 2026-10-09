import asyncio
import logging
import os
import shutil
import time
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Callable, List, Set, Tuple, Any
import config

logger = logging.getLogger(__name__)

class StreamSession:
    def __init__(
        self,
        channel_name: str,
        video_path: Optional[Path] = None,
        rtmp_url: str = "",
        loop: bool = True,
        notify_callback: Optional[Callable[[str], None]] = None,
        playlist_files: Optional[List[Path]] = None,
        playlist_names: Optional[List[str]] = None
    ):
        self.channel_name = channel_name
        self.rtmp_url = rtmp_url
        self.loop = loop
        self.notify_callback = notify_callback

        # Playlist setup
        self.playlist_files: List[Path] = playlist_files or ([video_path] if video_path else [])
        self.playlist_names: List[str] = playlist_names or [p.name for p in self.playlist_files]
        self.is_playlist: bool = len(self.playlist_files) > 1
        self.current_index: int = 0
        self.playlist_manifest_path: Optional[Path] = None

        # Primary video path (first file or current playlist track)
        self.video_path = self.playlist_files[0] if self.playlist_files else video_path

        self.process: Optional[asyncio.subprocess.Process] = None
        self.monitor_task: Optional[asyncio.Task] = None
        self.reader_task: Optional[asyncio.Task] = None
        self.start_time: float = 0.0
        self.status: str = "IDLE"  # IDLE, STARTING, LIVE, RECONNECTING, STOPPED, ERROR
        self.reconnect_count: int = 0
        self.is_manually_stopped: bool = False
        self.codec_mode: str = "auto"
        self.error_message: Optional[str] = None

        # Watchdog & Health Telemetry
        self.last_frame_time: float = time.time()
        self.fps: float = 0.0
        self.bitrate_kbps: float = 0.0
        self.speed: str = "1.0x"
        self.timecode: str = "00:00:00"
        self.stall_count: int = 0
        self.last_healed_at: Optional[str] = None
        self.health_state: str = "HEALTHY"  # HEALTHY, WARNING, STALLED, RECOVERING

    @property
    def now_playing(self) -> str:
        if self.is_playlist and self.playlist_names:
            idx = self.current_index % len(self.playlist_names)
            return self.playlist_names[idx]
        return self.video_path.name if self.video_path else "Live Video"

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

    def is_running(self) -> bool:
        """Determines if the session is actively transmitting or starting."""
        if self.is_manually_stopped:
            return False
        if self.status in ("STOPPED", "ERROR", "IDLE"):
            return False
        if self.status == "STARTING":
            return True
        if self.process and self.process.returncode is None:
            return True
        if self.status == "RECONNECTING":
            return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        """Returns structured session telemetry for dashboards and worker API."""
        return {
            "channel_name": self.channel_name,
            "status": self.status,
            "is_running": self.is_running(),
            "uptime": self.uptime_str,
            "uptime_str": self.uptime_str,
            "loop": self.loop,
            "codec_mode": self.codec_mode,
            "is_playlist": self.is_playlist,
            "playlist_count": len(self.playlist_files),
            "current_index": self.current_index,
            "now_playing": self.now_playing,
            "playlist_names": self.playlist_names,
            "fps": self.fps,
            "bitrate_kbps": self.bitrate_kbps,
            "speed": self.speed,
            "timecode": self.timecode,
            "stall_count": self.stall_count,
            "reconnect_count": self.reconnect_count,
            "health_state": self.health_state,
            "last_healed_at": self.last_healed_at,
            "error_message": self.error_message
        }


class StreamManager:
    """Manages active FFmpeg streaming processes with 24/7 Watchdog, Auto-Healing, and Playlists."""

    def __init__(self):
        self.sessions: Dict[str, StreamSession] = {}
        self.last_errors: Dict[str, str] = {}

    def get_last_error(self, channel_name: str) -> Optional[str]:
        """Returns the most recent startup failure error for a channel."""
        return self.last_errors.get(channel_name)

    def get_active_video_paths(self) -> Set[str]:
        """Returns paths of video files currently in use by active streams."""
        active_paths = set()
        for s in self.list_active().values():
            if s.is_running() or s.status in ("STARTING", "LIVE", "RECONNECTING"):
                for p in s.playlist_files:
                    active_paths.add(str(p))
                if s.video_path:
                    active_paths.add(str(s.video_path))
        return active_paths

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
        if not ffprobe_bin or not video_path or not video_path.exists():
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

            fps_str = v_stream.get("r_frame_rate", "30/1")
            if "/" in fps_str:
                num, den = fps_str.split("/")
                fps = float(num) / float(den) if float(den) > 0 else 30.0
            else:
                fps = float(fps_str or 30.0)

            has_audio = bool(a_stream)
            is_aac = acodec.lower() in ("aac", "mp4a")

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
            logger.debug(f"ffprobe failed for {video_path}: {e}")
            return default_info

    def create_playlist_manifest(self, session: StreamSession) -> Path:
        """Generates an FFmpeg concat demuxer manifest file for smooth multi-video transition."""
        config.PLAYLIST_DIR.mkdir(parents=True, exist_ok=True)
        manifest_path = config.PLAYLIST_DIR / f"playlist_{session.channel_name}.txt"

        # Order starting from current_index
        count = len(session.playlist_files)
        ordered_files = [
            session.playlist_files[(session.current_index + i) % count]
            for i in range(count)
        ]

        lines = ["ffconcat version 1.0"]
        for p in ordered_files:
            # FFconcat expects forward-slashed paths safely formatted
            clean_p = str(p.resolve()).replace('\\', '/')
            lines.append(f"file '{clean_p}'")

        with open(manifest_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        session.playlist_manifest_path = manifest_path
        logger.info(f"Generated playlist manifest for [{session.channel_name}] with {len(ordered_files)} files at {manifest_path}")
        return manifest_path

    def build_ffmpeg_cmd(self, session: StreamSession, video_info: dict, quality_mode: str = "1080p") -> list:
        """Constructs FFmpeg command supporting Single Video, Concat Playlists, and strict 2s GOP."""
        ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
        cmd = [ffmpeg_bin, "-re"]

        # Input handling: Playlist Concat or Single File
        if session.is_playlist and len(session.playlist_files) > 1:
            manifest_path = self.create_playlist_manifest(session)
            if session.loop:
                cmd.extend(["-stream_loop", "-1"])
            cmd.extend([
                "-f", "concat",
                "-safe", "0",
                "-i", str(manifest_path)
            ])
        else:
            if session.loop:
                cmd.extend(["-stream_loop", "-1"])
            cmd.extend(["-i", str(session.video_path)])

        height = int(video_info.get("height", 0))
        width = int(video_info.get("width", 0))
        has_audio = video_info.get("has_audio", True)
        is_aac = video_info.get("is_aac", True)
        vcodec = video_info.get("vcodec", "").lower()
        can_copy_v = (vcodec in ("h264", "avc1"))

        # Meta Facebook Live & YouTube strictly mandate 48 kHz AAC audio
        audio_args = ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]

        # Mode handling
        if quality_mode == "copy" and not session.is_playlist and can_copy_v:
            # Direct stream-copy only for single files with H.264 video codec
            cmd.extend(["-c:v", "copy"])
            if has_audio:
                if is_aac:
                    cmd.extend(["-c:a", "copy"])
                else:
                    cmd.extend(audio_args)
            else:
                cmd.extend(["-an"])
            session.codec_mode = "Stream-Copy (Direct pass)"

        elif quality_mode == "720p" or (quality_mode == "copy" and not can_copy_v and (height <= 720 and width <= 1280)):
            # 720p HD mode with strict Constant Bitrate (CBR) and 2-second keyframe cadence (GOP = 60 @ 30fps)
            vf_filter = "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=30" if session.is_playlist else "scale=-2:720,fps=30"
            cmd.extend([
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-tune", "zerolatency",
                "-vf", vf_filter,
                "-r", "30",
                "-b:v", "2800k",
                "-minrate", "2800k",
                "-maxrate", "2800k",
                "-bufsize", "2800k",
                "-x264-params", "nal-hrd=cbr:force-cfr=1",
                "-pix_fmt", "yuv420p",
                "-g", "60",
                "-keyint_min", "60",
                "-sc_threshold", "0",
            ])
            if has_audio:
                cmd.extend(audio_args)
            else:
                cmd.extend(["-an"])
            session.codec_mode = "720p HD"

        else:
            # 1080p Full HD mode (Default) with strict Constant Bitrate (CBR)
            if session.is_playlist:
                vf_filter = "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps=30"
            elif height > 1080 or width > 1920:
                vf_filter = "scale=-2:1080,fps=30"
            else:
                vf_filter = "scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=30"

            scale_args = ["-vf", vf_filter] if vf_filter else []
            bitrate = "4500k" if (height >= 1080 or width >= 1920 or height == 0 or session.is_playlist) else "2800k"
            mode_name = "1080p Full HD" if bitrate == "4500k" else "720p HD"

            cmd.extend([
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-tune", "zerolatency",
                *scale_args,
                "-r", "30",
                "-b:v", bitrate,
                "-minrate", bitrate,
                "-maxrate", bitrate,
                "-bufsize", bitrate,
                "-x264-params", "nal-hrd=cbr:force-cfr=1",
                "-pix_fmt", "yuv420p",
                "-g", "60",
                "-keyint_min", "60",
                "-sc_threshold", "0",
            ])
            if has_audio:
                cmd.extend(audio_args)
            else:
                cmd.extend(["-an"])
            session.codec_mode = f"{mode_name}"

        # RTMP FLV output with packet flushing
        cmd.extend([
            "-fflags", "+nobuffer+flush_packets",
            "-flvflags", "no_duration_filesize",
            "-f", "flv",
            session.rtmp_url
        ])

        return cmd

    async def _read_ffmpeg_output(self, session: StreamSession, proc: asyncio.subprocess.Process, log_file):
        """Telemetry reader: parses real-time FPS, bitrate, and timecode for the Watchdog."""
        session.last_frame_time = time.time()
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="ignore")
                log_file.write(line)
                log_file.flush()

                # Detect active video encoding frames
                if "frame=" in text or "time=" in text or "bitrate=" in text:
                    session.last_frame_time = time.time()
                    session.health_state = "HEALTHY"

                    # FPS
                    fps_m = re.search(r'fps=\s*([0-9.]+)', text)
                    if fps_m:
                        try:
                            session.fps = float(fps_m.group(1))
                        except ValueError:
                            pass

                    # Bitrate
                    br_m = re.search(r'bitrate=\s*([0-9.]+)\s*kbits/s', text)
                    if br_m:
                        try:
                            session.bitrate_kbps = float(br_m.group(1))
                        except ValueError:
                            pass

                    # Speed
                    spd_m = re.search(r'speed=\s*([0-9.x]+)', text)
                    if spd_m:
                        session.speed = spd_m.group(1).strip()

                    # Timecode
                    tm_m = re.search(r'time=\s*([0-9:.]+)', text)
                    if tm_m:
                        session.timecode = tm_m.group(1).split(".")[0]
        except Exception as e:
            logger.debug(f"Telemetry reader finished for [{session.channel_name}]: {e}")
        finally:
            try:
                log_file.close()
            except Exception:
                pass

    async def _launch_process(self, session: StreamSession) -> bool:
        """Launches FFmpeg and attaches the real-time telemetry reader."""
        from settings_manager import settings_manager

        # Cancel any previous reader task
        if session.reader_task and not session.reader_task.done():
            session.reader_task.cancel()

        target_for_probe = session.video_path if session.video_path else (session.playlist_files[0] if session.playlist_files else None)
        video_info = await self.probe_video_info(target_for_probe) if target_for_probe else {}
        quality_mode = settings_manager.get_quality()
        cmd = self.build_ffmpeg_cmd(session, video_info=video_info, quality_mode=quality_mode)

        log_file_path = config.LOGS_DIR / f"{session.channel_name}_ffmpeg.log"
        logger.info(f"Launching FFmpeg for [{session.channel_name}]: {' '.join(cmd[:8])} ...")

        try:
            log_file = open(log_file_path, "wb")
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE
            )
            session.process = process
            session.start_time = time.time()
            session.last_frame_time = time.time()
            session.status = "LIVE"
            session.health_state = "HEALTHY"

            # Spawn real-time progress reader
            session.reader_task = asyncio.create_task(self._read_ffmpeg_output(session, process, log_file))

            # Quick verification that process didn't instantly crash (e.g. invalid RTMP key or closed socket)
            await asyncio.sleep(1.5)
            if process.returncode is not None:
                err_detail = ""
                if log_file_path.exists():
                    try:
                        raw = log_file_path.read_text(encoding="utf-8", errors="ignore").strip().splitlines()
                        err_lines = [l.strip() for l in raw if l.strip() and not l.strip().startswith("frame=")]
                        for l in reversed(err_lines):
                            if any(k in l.lower() for k in ("error", "failed", "cannot", "invalid", "refused", "reset", "denied", "i/o", "end of file", "handshake", "tls", "connection")):
                                err_detail = l
                                break
                        if not err_detail and err_lines:
                            err_detail = err_lines[-1]
                    except Exception:
                        pass

                if any(k in err_detail.lower() for k in ("tls fatal", "handshake")):
                    clean_err = f"Stream key rejected or expired by platform ({err_detail}). In Facebook Live Producer, please check that your live post is open or generate a new stream key."
                elif "input/output error" in err_detail.lower():
                    clean_err = f"Remote RTMP connection rejected ({err_detail}). Stream key may have already ended or expired."
                else:
                    clean_err = f"FFmpeg exited (code {process.returncode}): {err_detail}" if err_detail else f"FFmpeg exited immediately with code {process.returncode}."
                session.status = "ERROR"
                session.error_message = clean_err
                self.last_errors[session.channel_name] = clean_err
                logger.error(f"FFmpeg for [{session.channel_name}] exited immediately: {clean_err}")
                return False

            return True
        except Exception as e:
            session.status = "ERROR"
            session.error_message = str(e)
            self.last_errors[session.channel_name] = str(e)
            logger.error(f"Failed to start FFmpeg for [{session.channel_name}]: {e}")
            return False

    async def _monitor_stream(self, session: StreamSession):
        """24/7 Watchdog: proactively detects stalls (frozen 0 fps), drops, and auto-heals."""
        stall_timeout = getattr(config, "STALL_TIMEOUT_SECONDS", 18)
        check_interval = getattr(config, "WATCHDOG_CHECK_INTERVAL", 3)
        max_auto_heal = getattr(config, "WATCHDOG_MAX_AUTO_HEAL", 50)
        max_consecutive = 3
        consecutive_failures = 0

        logger.info(f"🛡 Stream Watchdog active for channel [{session.channel_name}] (Stall timeout: {stall_timeout}s)")

        try:
            while not session.is_manually_stopped:
                await asyncio.sleep(check_interval)
                if session.is_manually_stopped:
                    session.status = "STOPPED"
                    break

                proc = session.process
                if not proc:
                    session.status = "STOPPED"
                    break

                # 1. Process termination check
                if proc.returncode is not None:
                    return_code = proc.returncode
                    logger.warning(f"FFmpeg process for channel [{session.channel_name}] exited with code {return_code}")

                    if session.is_manually_stopped:
                        session.status = "STOPPED"
                        break

                    # Natural finish for single-play (loop=False) videos
                    if not session.loop and return_code == 0:
                        session.status = "STOPPED"
                        logger.info(f"Stream for [{session.channel_name}] finished naturally (single play completed).")
                        if session.notify_callback:
                            asyncio.create_task(session.notify_callback(
                                f"🏁 Broadcast for <b>[{session.channel_name}]</b> has finished."
                            ))
                        break

                    consecutive_failures += 1
                    if config.AUTO_RECONNECT and session.reconnect_count < max_auto_heal and consecutive_failures <= max_consecutive:
                        session.reconnect_count += 1
                        session.status = "RECONNECTING"
                        session.health_state = "RECOVERING"
                        session.last_healed_at = datetime.now().strftime("%H:%M:%S")

                        alert_msg = (
                            f"⚠️ Live stream for <b>[{session.channel_name}]</b> interrupted (code {return_code}).\n"
                            f"🔄 <b>Watchdog Auto-Healing:</b> Reconnecting attempt {consecutive_failures}/{max_consecutive}..."
                        )
                        logger.info(alert_msg)
                        if session.notify_callback:
                            asyncio.create_task(session.notify_callback(alert_msg))

                        await asyncio.sleep(config.RECONNECT_DELAY_SECONDS)
                        if session.is_manually_stopped:
                            session.status = "STOPPED"
                            break

                        success = await self._launch_process(session)
                        if success:
                            succ_msg = f"✅ Stream for <b>[{session.channel_name}]</b> auto-healed & broadcast restored to LIVE!"
                            logger.info(succ_msg)
                            if session.notify_callback:
                                asyncio.create_task(session.notify_callback(succ_msg))
                            continue
                        else:
                            if consecutive_failures >= max_consecutive:
                                session.status = "STOPPED"
                                err_msg = (
                                    f"❌ Stream for <b>[{session.channel_name}]</b> disconnected.\n"
                                    f"Remote RTMP broadcast disconnected or stream ended by user."
                                )
                                logger.warning(err_msg)
                                if session.notify_callback:
                                    asyncio.create_task(session.notify_callback(err_msg))
                                break
                            continue
                    else:
                        session.status = "STOPPED"
                        err_msg = (
                            f"❌ Stream for <b>[{session.channel_name}]</b> stopped.\n"
                            f"Remote RTMP broadcast disconnected or maximum retries reached."
                        )
                        logger.warning(err_msg)
                        if session.notify_callback:
                            asyncio.create_task(session.notify_callback(err_msg))
                        break

                # If process is running stably and emitting frames, reset consecutive failures
                if proc.returncode is None and session.status == "LIVE" and (time.time() - session.start_time > 15):
                    consecutive_failures = 0

                # 2. Freeze / Stall detection (process alive, but 0 frames emitted for > stall_timeout)
                time_since_frame = time.time() - session.last_frame_time
                if time_since_frame > stall_timeout and session.status == "LIVE":
                    session.stall_count += 1
                    session.health_state = "STALLED"
                    session.last_healed_at = datetime.now().strftime("%H:%M:%S")
                    logger.warning(
                        f"🚨 Watchdog Freeze Detected on [{session.channel_name}]! "
                        f"No video frames emitted for {time_since_frame:.1f}s. Auto-healing now..."
                    )

                    stall_msg = (
                        f"⚠️ <b>Stream Stall Detected on [{session.channel_name}]</b>\n"
                        f"Video froze at 0 fps. Watchdog is auto-healing and restarting the broadcast..."
                    )
                    if session.notify_callback:
                        asyncio.create_task(session.notify_callback(stall_msg))

                    # Terminate hung process
                    try:
                        proc.terminate()
                        try:
                            await asyncio.wait_for(proc.wait(), timeout=2.5)
                        except asyncio.TimeoutError:
                            proc.kill()
                            await proc.wait()
                    except Exception as e:
                        logger.debug(f"Error terminating stalled process: {e}")

                    await asyncio.sleep(1.0)
                    if session.is_manually_stopped:
                        session.status = "STOPPED"
                        break
                    session.reconnect_count += 1
                    success = await self._launch_process(session)
                    if success:
                        restored_msg = f"🟢 <b>Stream [{session.channel_name}] Restored!</b> Auto-healed from freeze and streaming smoothly."
                        logger.info(restored_msg)
                        if session.notify_callback:
                            asyncio.create_task(session.notify_callback(restored_msg))
        finally:
            # Ensure cleanup when watchdog loop terminates for any reason
            session.status = "STOPPED" if session.status not in ("ERROR",) else session.status
            if session.reader_task and not session.reader_task.done():
                session.reader_task.cancel()
            if session.process and session.process.returncode is None:
                try:
                    session.process.terminate()
                except Exception:
                    pass
            # Clean playlist manifest file if one was generated
            if session.playlist_manifest_path and session.playlist_manifest_path.exists():
                try:
                    session.playlist_manifest_path.unlink()
                except Exception:
                    pass
            # Purge from active sessions so active stream count drops to 0 immediately
            if session.channel_name in self.sessions:
                try:
                    del self.sessions[session.channel_name]
                    logger.info(f"🧹 Purged inactive stream [{session.channel_name}]. Remaining active sessions: {len(self.sessions)}")
                except KeyError:
                    pass

    async def start_stream(
        self,
        channel_name: str,
        video_path: Optional[Path] = None,
        rtmp_url: str = "",
        loop: bool = True,
        notify_callback: Optional[Callable[[str], None]] = None,
        playlist_files: Optional[List[Path]] = None,
        playlist_names: Optional[List[str]] = None
    ) -> bool:
        """Starts a live stream (Single Video or Playlist)."""
        if channel_name in self.sessions:
            await self.stop_stream(channel_name)

        session = StreamSession(
            channel_name=channel_name,
            video_path=video_path,
            rtmp_url=rtmp_url,
            loop=loop,
            notify_callback=notify_callback,
            playlist_files=playlist_files,
            playlist_names=playlist_names
        )
        session.status = "STARTING"
        self.sessions[channel_name] = session

        success = await self._launch_process(session)
        if success:
            session.monitor_task = asyncio.create_task(self._monitor_stream(session))
            self.last_errors.pop(channel_name, None)
            return True
        else:
            if not self.last_errors.get(channel_name):
                self.last_errors[channel_name] = session.error_message or "Failed to start FFmpeg process"
            if channel_name in self.sessions:
                try:
                    del self.sessions[channel_name]
                except KeyError:
                    pass
            return False

    async def skip_next(self, channel_name: str) -> Tuple[bool, str]:
        """Skips to the next video in a playlist with gapless transition."""
        session = self.get_session(channel_name)
        if not session:
            return False, f"Channel [{channel_name}] is not currently streaming."

        if not session.is_playlist or len(session.playlist_files) <= 1:
            return False, "Active stream is not a multi-video playlist."

        session.current_index = (session.current_index + 1) % len(session.playlist_files)
        next_track = session.playlist_names[session.current_index]

        # Stop current process and relaunch with rotated manifest
        if session.process:
            try:
                session.process.terminate()
                try:
                    await asyncio.wait_for(session.process.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    session.process.kill()
            except Exception:
                pass

        success = await self._launch_process(session)
        if success:
            logger.info(f"Skipped playlist [{channel_name}] to track {session.current_index + 1}: {next_track}")
            return True, f"Skipped to next video: <b>{next_track}</b>"
        return False, "Failed to start next video in playlist."

    async def stop_stream(self, channel_name: str) -> bool:
        """Gracefully stops an active stream and cancels watchdog tasks."""
        session = self.sessions.get(channel_name)
        if not session:
            return False

        session.is_manually_stopped = True
        session.status = "STOPPED"

        if session.reader_task and not session.reader_task.done():
            session.reader_task.cancel()

        if session.process and session.process.returncode is None:
            try:
                session.process.terminate()
                try:
                    await asyncio.wait_for(session.process.wait(), timeout=4.0)
                except asyncio.TimeoutError:
                    session.process.kill()
            except Exception as e:
                logger.error(f"Error terminating stream [{channel_name}]: {e}")

        if session.monitor_task and not session.monitor_task.done():
            session.monitor_task.cancel()

        # Clean playlist manifest file if one was generated
        if session.playlist_manifest_path and session.playlist_manifest_path.exists():
            try:
                session.playlist_manifest_path.unlink()
            except Exception:
                pass

        del self.sessions[channel_name]
        return True

    async def restart_stream(self, channel_name: str) -> bool:
        """Restarts an active stream from 0:00 without losing destination or playlist settings."""
        session = self.sessions.get(channel_name)
        if not session:
            return False

        video_path = session.video_path
        rtmp_url = session.rtmp_url
        loop = session.loop
        notify_callback = session.notify_callback
        playlist_files = session.playlist_files
        playlist_names = session.playlist_names

        await self.stop_stream(channel_name)
        await asyncio.sleep(0.5)

        return await self.start_stream(
            channel_name=channel_name,
            video_path=video_path,
            rtmp_url=rtmp_url,
            loop=loop,
            notify_callback=notify_callback,
            playlist_files=playlist_files,
            playlist_names=playlist_names
        )

    async def stop_all_streams(self) -> int:
        """Stops all running streams. Returns count of stopped streams."""
        channel_names = list(self.sessions.keys())
        for ch in channel_names:
            await self.stop_stream(ch)
        return len(channel_names)

    def get_session(self, channel_name: str) -> Optional[StreamSession]:
        s = self.sessions.get(channel_name)
        if s and not s.is_running() and s.status != "RECONNECTING":
            try:
                del self.sessions[channel_name]
            except KeyError:
                pass
            return None
        return s

    def get_session_info(self, channel_name: str) -> Optional[Dict[str, Any]]:
        """Returns structured session telemetry for dashboards and worker API."""
        s = self.get_session(channel_name)
        if not s:
            return None
        return s.to_dict()

    def list_active(self) -> Dict[str, StreamSession]:
        """Returns all genuinely active sessions, pruning any dead sessions."""
        dead_keys = []
        for name, s in list(self.sessions.items()):
            if not s.is_running() and s.status != "RECONNECTING":
                dead_keys.append(name)
        for k in dead_keys:
            try:
                del self.sessions[k]
                logger.debug(f"Pruned dead stream session [{k}]")
            except KeyError:
                pass
        return self.sessions

# Global StreamManager instance
stream_manager = StreamManager()
