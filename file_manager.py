import os
import psutil
import logging
import hashlib
import urllib.parse
import html
from pathlib import Path
from typing import List, Dict, Any, Optional
import config
from streamer import stream_manager

logger = logging.getLogger(__name__)

class FileManager:
    """Manages downloaded video files on the server disk."""

    def __init__(self, download_dir: Optional[Path] = None):
        self.download_dir = download_dir or config.DOWNLOAD_DIR
        self.download_dir.mkdir(parents=True, exist_ok=True)

    def get_storage_stats(self) -> Dict[str, Any]:
        """Returns storage metrics for the disk and download directory."""
        disk = psutil.disk_usage(str(config.DATA_DIR))
        files = self.list_files()
        total_files_size = sum(f["size_bytes"] for f in files)

        return {
            "disk_total_gb": disk.total / (1024 ** 3),
            "disk_used_gb": disk.used / (1024 ** 3),
            "disk_free_gb": disk.free / (1024 ** 3),
            "disk_free_percent": (disk.free / disk.total) * 100,
            "total_files": len(files),
            "files_size_mb": total_files_size / (1024 ** 2),
            "files_size_gb": total_files_size / (1024 ** 3),
        }

    def list_files(self) -> List[Dict[str, Any]]:
        """Returns sorted list of downloaded video files (newest first)."""
        active_paths = stream_manager.get_active_video_paths()
        files = []

        if not self.download_dir.exists():
            return []

        for p in self.download_dir.glob("*"):
            if p.is_file() and not p.name.endswith(".downloading"):
                try:
                    stat = p.stat()
                    size_bytes = stat.st_size
                    size_mb = size_bytes / (1024 * 1024)
                    size_gb = size_bytes / (1024 * 1024 * 1024)
                    if size_gb >= 1.0:
                        size_str = f"{size_gb:.2f} GB"
                    else:
                        size_str = f"{size_mb:.1f} MB"

                    parts = p.name.split("_", 1)
                    if len(parts) == 2 and len(parts[0]) == 12 and all(c in "0123456789abcdef" for c in parts[0]):
                        file_id = parts[0]
                        clean_name = parts[1]
                    else:
                        file_id = hashlib.sha256(p.name.encode()).hexdigest()[:12]
                        clean_name = p.name

                    clean_name = urllib.parse.unquote(clean_name)
                    is_active = str(p) in active_paths

                    files.append({
                        "file_id": file_id,
                        "filename": p.name,
                        "display_name": clean_name,
                        "path": p,
                        "size_bytes": size_bytes,
                        "size_mb": size_mb,
                        "size_str": size_str,
                        "mtime": stat.st_mtime,
                        "is_active": is_active,
                    })
                except Exception as e:
                    logger.debug(f"Error reading file stat {p}: {e}")

        # Sort newest first
        files.sort(key=lambda x: x["mtime"], reverse=True)
        return files

    def get_file(self, identifier: str) -> Optional[Path]:
        """Finds a file by exact filename, file_id prefix, or sha256 hash."""
        target = self.download_dir / identifier
        if target.exists() and target.is_file() and not target.name.endswith(".downloading"):
            return target

        # Search by file_id prefix or full name
        for p in self.download_dir.glob("*"):
            if p.is_file() and not p.name.endswith(".downloading"):
                if p.name == identifier or p.name.startswith(f"{identifier}_") or p.name.startswith(identifier):
                    return p
                if hashlib.sha256(p.name.encode()).hexdigest()[:12] == identifier:
                    return p
        return None

    def delete_file(self, filename: str) -> tuple[bool, str]:
        """Deletes a downloaded video file if not actively streaming."""
        file_path = self.get_file(filename)
        if not file_path:
            return False, "File not found."

        active_paths = stream_manager.get_active_video_paths()
        if str(file_path) in active_paths:
            return False, "Cannot delete: File is currently being streamed!"

        try:
            size_mb = file_path.stat().st_size / (1024 * 1024)
            file_name = file_path.name
            file_path.unlink()
            logger.info(f"Deleted file {file_name} ({size_mb:.1f} MB freed)")
            return True, f"Deleted <code>{html.escape(file_name)}</code> ({size_mb:.1f} MB freed)."
        except Exception as e:
            logger.error(f"Failed to delete {filename}: {e}")
            return False, f"Failed to delete: {e}"

    def delete_all_inactive(self) -> tuple[int, float]:
        """Deletes all downloaded videos and temporary files that are not currently being streamed."""
        active_paths = stream_manager.get_active_video_paths()
        deleted_count = 0
        freed_bytes = 0

        # Delete inactive downloaded files
        for f in self.list_files():
            if not f["is_active"]:
                try:
                    freed_bytes += f["size_bytes"]
                    f["path"].unlink()
                    deleted_count += 1
                except Exception as e:
                    logger.error(f"Failed to delete {f['filename']}: {e}")

        # Clean orphaned partial downloads
        for p in self.download_dir.glob("*.downloading"):
            try:
                freed_bytes += p.stat().st_size
                p.unlink()
                deleted_count += 1
            except Exception:
                pass

        freed_mb = freed_bytes / (1024 * 1024)
        return deleted_count, freed_mb

file_manager = FileManager()
