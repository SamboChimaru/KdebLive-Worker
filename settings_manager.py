import os
import json
import logging
from typing import Dict, Any, Optional
import config

logger = logging.getLogger(__name__)

SETTINGS_FILE = config.DATA_DIR / "settings.json"

DEFAULT_ICONS = {
    # Main Menu & Dashboard
    "yt": "🔴",
    "fb": "🔵",
    "status": "📊",
    "refresh": "🔄",
    "stop": "🛑",
    "loop": "🔁",
    "channels": "📺",
    "files": "📁",
    "server": "🖥",
    "quality": "🎥",
    "help": "❓",
    "live": "🟢",
    "admin": "👑",
    # Sub-Menu Buttons & Actions
    "back": "🔙",
    "cancel": "❌",
    "add": "➕",
    "delete": "🗑",
    "play": "▶️",
    "clean": "🧹",
    "restart": "🔄",
    "stop_ch": "⏹",
    "stop_all": "🛑",
    "rocket": "🚀",
    "users": "👥",
    "prev": "⬅️",
    "next": "➡️",
    "done": "💾",
}

ICON_LABELS = {
    # Main Menu & Dashboard
    "yt": "Live YouTube",
    "fb": "Live Facebook",
    "status": "Status",
    "refresh": "Refresh Dashboard",
    "stop": "Stop Stream",
    "loop": "Toggle Loop",
    "channels": "Channels",
    "files": "File Manager",
    "server": "Server Health",
    "quality": "Quality Mode",
    "help": "Help",
    "live": "LIVE Indicator",
    "admin": "Admin Panel",
    # Sub-Menu Buttons & Actions
    "back": "Back (to Dashboard / Menu)",
    "cancel": "Cancel Action",
    "add": "Add (User / Server / Channel)",
    "delete": "Delete (File / Server / User)",
    "play": "Play / Stream Saved File",
    "clean": "Delete All Inactive Files",
    "restart": "Restart Video (from 0:00)",
    "stop_ch": "Stop Single Channel",
    "stop_all": "Stop ALL Streams",
    "rocket": "Start Live Stream (Launch)",
    "users": "User & Moderator Manager",
    "prev": "Previous Page",
    "next": "Next Page",
    "done": "Done / Save",
}

class SettingsManager:
    """Manages bot UI customizations, persistence, and Telegram Premium custom emojis."""

    def __init__(self):
        self._settings: Dict[str, Any] = {
            f"{k}_emoji_id": None for k in DEFAULT_ICONS
        }
        self.load()

    def load(self):
        # 1. Load from environment variables first (ideal for Railway persistent variables)
        env_json = os.getenv("SETTINGS_JSON", "").strip()
        if env_json:
            data = None
            try:
                data = json.loads(env_json)
                if isinstance(data, str):
                    data = json.loads(data)
            except Exception:
                try:
                    import ast
                    data = ast.literal_eval(env_json)
                except Exception as e:
                    logger.warning(f"Failed to parse SETTINGS_JSON environment variable: {e}")

            if isinstance(data, dict):
                for k, v in data.items():
                    if v is not None and str(v).strip():
                        val_str = str(v).strip()
                        if k.endswith("_emoji_id"):
                            self._settings[k] = val_str
                        elif k in DEFAULT_ICONS:
                            self._settings[f"{k}_emoji_id"] = val_str
                        elif k == "stream_quality":
                            self._settings["stream_quality"] = val_str
                logger.info("Loaded custom settings from SETTINGS_JSON environment variable")

        # Check individual EMOJI_<TARGET> or <TARGET>_EMOJI_ID env vars
        for k in DEFAULT_ICONS:
            val = os.getenv(f"EMOJI_{k.upper()}") or os.getenv(f"{k.upper()}_EMOJI_ID")
            if val and val.strip():
                self._settings[f"{k}_emoji_id"] = val.strip()

        # 2. Load from disk file if exists (overrides / preserves live edits)
        if SETTINGS_FILE.exists():
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    file_data = json.load(f)
                    for key, val in file_data.items():
                        if val is not None or key not in self._settings:
                            self._settings[key] = val
                    logger.info(f"Loaded settings from {SETTINGS_FILE}")
            except Exception as e:
                logger.error(f"Error loading settings from {SETTINGS_FILE}: {e}")

    def save(self):
        try:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self._settings, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving settings: {e}")

    def set_emoji(self, target: str, emoji_id: str):
        """Sets custom emoji ID for any target."""
        key = f"{target}_emoji_id"
        self._settings[key] = str(emoji_id).strip()
        self.save()

    def get_raw_value(self, target: str) -> Optional[str]:
        """Returns the raw stored value or environment variable without filtering."""
        val = self._settings.get(f"{target}_emoji_id")
        if val is None:
            val = os.getenv(f"EMOJI_{target.upper()}") or os.getenv(f"{target.upper()}_EMOJI_ID")
        if val:
            v_str = str(val).strip()
            if v_str and v_str.lower() not in ("none", "null", "default", "false"):
                return v_str
        return None

    def get_default_glyph(self, target: str) -> str:
        """Returns the standard default unicode icon."""
        return DEFAULT_ICONS.get(target, "✨")

    def get_glyph(self, target: str) -> str:
        """Returns the unicode icon: custom unicode glyph if user specified one (e.g. 🔥), else default."""
        raw = self.get_raw_value(target)
        if raw and not raw.isdigit() and len(raw) <= 4 and raw.lower() not in ("your_emoji_id", "placeholder"):
            return raw
        return self.get_default_glyph(target)

    def get_emoji_id(self, target: str) -> Optional[str]:
        """Returns valid numeric custom emoji ID, or None if not set or invalid."""
        raw = self.get_raw_value(target)
        if raw and raw.isdigit():
            return raw
        return None

    def get_icon(self, target: str) -> str:
        """Returns formatted custom emoji tag if configured with valid ID, or fallback Unicode emoji."""
        emoji_id = self.get_emoji_id(target)
        glyph = self.get_glyph(target)
        if emoji_id:
            return f'<tg-emoji emoji-id="{emoji_id}">{glyph}</tg-emoji>'
        return glyph

    def reset_emoji(self, target: str):
        key = f"{target}_emoji_id"
        self._settings[key] = None
        self.save()

    def reset_all(self):
        for k in DEFAULT_ICONS:
            self._settings[f"{k}_emoji_id"] = None
        self.save()

    def import_settings(self, data: dict) -> int:
        """Imports emoji settings from dictionary, returns count of imported emojis."""
        imported_count = 0
        for k, v in data.items():
            if k.endswith("_emoji_id") and v:
                self._settings[k] = str(v).strip()
                imported_count += 1
            elif k in DEFAULT_ICONS and v:
                self._settings[f"{k}_emoji_id"] = str(v).strip()
                imported_count += 1
            elif k == "stream_quality" and v:
                self._settings["stream_quality"] = str(v)
        if imported_count > 0:
            self.save()
        return imported_count

    def export_json(self) -> str:
        """Exports current emoji settings as a clean JSON string."""
        clean = {k: v for k, v in self._settings.items() if v is not None}
        return json.dumps(clean, indent=2)

    def export_env_vars(self) -> str:
        """Exports emoji settings as environment variable definitions for Railway."""
        lines = []
        for k in DEFAULT_ICONS:
            eid = self.get_emoji_id(k)
            if eid:
                lines.append(f"EMOJI_{k.upper()}={eid}")
        return "\n".join(lines)

    # Specific convenience getters
    def get_yt_icon(self) -> str:
        return self.get_icon("yt")

    def get_fb_icon(self) -> str:
        return self.get_icon("fb")

    def get_live_icon(self) -> str:
        return self.get_icon("live")

    # Stream quality management
    def get_quality(self) -> str:
        """Returns stream quality mode: '1080p', '720p', or 'copy'."""
        return self._settings.get("stream_quality", "1080p")

    def set_quality(self, mode: str):
        if mode in ("1080p", "720p", "copy"):
            self._settings["stream_quality"] = mode
            self.save()

    def get_quality_label(self) -> str:
        labels = {
            "1080p": "1080p Full HD",
            "720p": "720p HD",
            "copy": "Stream-Copy (Ultra-low CPU)"
        }
        return labels.get(self.get_quality(), "1080p Full HD")

settings_manager = SettingsManager()
