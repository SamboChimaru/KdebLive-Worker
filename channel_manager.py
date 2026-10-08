import json
import logging
from typing import Dict, Any, Optional
import config

logger = logging.getLogger(__name__)

PLATFORM_ENDPOINTS = {
    "youtube": "rtmp://a.rtmp.youtube.com/live2/{stream_key}",
    "facebook": "rtmps://live-api-s.facebook.com:443/rtmp/{stream_key}",
    "twitch": "rtmp://live.twitch.tv/app/{stream_key}",
}

class ChannelManager:
    """Manages saved streaming channel configurations (YouTube, Facebook, etc.)."""

    def __init__(self):
        self.channels_file = config.CHANNELS_FILE
        self._channels: Dict[str, Dict[str, Any]] = {}
        self.load()

    def load(self):
        """Loads channels from the JSON storage file."""
        if self.channels_file.exists():
            try:
                with open(self.channels_file, "r", encoding="utf-8") as f:
                    self._channels = json.load(f)
            except Exception as e:
                logger.error(f"Error loading channels from {self.channels_file}: {e}")
                self._channels = {}
        else:
            self._channels = {}

    def save(self):
        """Saves channels to the JSON storage file."""
        try:
            with open(self.channels_file, "w", encoding="utf-8") as f:
                json.dump(self._channels, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving channels: {e}")

    def add_channel(self, name: str, platform: str, stream_key: str, custom_url: Optional[str] = None) -> bool:
        """Adds or updates a channel."""
        name = name.strip().lower()
        platform = platform.strip().lower()
        stream_key = stream_key.strip()

        if platform not in PLATFORM_ENDPOINTS and platform != "custom":
            raise ValueError(f"Unsupported platform: {platform}. Supported: youtube, facebook, twitch, custom")

        if platform == "custom" and not custom_url:
            raise ValueError("custom_url is required when platform is 'custom'")

        self._channels[name] = {
            "name": name,
            "platform": platform,
            "stream_key": stream_key,
            "custom_url": custom_url.strip() if custom_url else None
        }
        self.save()
        return True

    def remove_channel(self, name: str) -> bool:
        """Deletes a channel by name."""
        name = name.strip().lower()
        if name in self._channels:
            del self._channels[name]
            self.save()
            return True
        return False

    def get_channel(self, name: str) -> Optional[Dict[str, Any]]:
        """Retrieves channel configuration."""
        return self._channels.get(name.strip().lower())

    def list_channels(self) -> Dict[str, Dict[str, Any]]:
        """Returns all configured channels."""
        return self._channels

    def get_rtmp_url(self, name: str) -> Optional[str]:
        """Resolves the complete RTMP/RTMPS destination URL for a channel."""
        ch = self.get_channel(name)
        if not ch:
            return None

        platform = ch["platform"]
        key = ch["stream_key"]

        if platform in PLATFORM_ENDPOINTS:
            return PLATFORM_ENDPOINTS[platform].format(stream_key=key)
        elif platform == "custom":
            custom_url = ch.get("custom_url", "").rstrip("/")
            if "{stream_key}" in custom_url:
                return custom_url.format(stream_key=key)
            return f"{custom_url}/{key}"
        return None

# Global channel manager instance
channel_manager = ChannelManager()
