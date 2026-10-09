import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Base directories
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DOWNLOAD_DIR = DATA_DIR / "downloads"
CHANNELS_FILE = DATA_DIR / "channels.json"
USERS_FILE = DATA_DIR / "users.json"
SERVERS_FILE = DATA_DIR / "servers.json"
COUPONS_FILE = DATA_DIR / "coupons.json"
LOGS_DIR = DATA_DIR / "logs"

# Ensure data directories exist
DATA_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Telegram Settings
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Web Plans Editor Passkey (configurable via Railway environment variables)
WEB_PASSKEY = (
    os.getenv("WEB_PASSKEY")
    or os.getenv("WEB_EDITOR_PASSKEY")
    or os.getenv("WEB_EDITOR_PASSCODE")
    or os.getenv("WEB_PASSWORD")
    or os.getenv("ADMIN_PASSKEY")
    or os.getenv("ADMIN_PASSCODE")
    or "Chimaru0011@@"
).strip().strip("'").strip('"')

# Whitelist Telegram User IDs (comma-separated list of ints)
_raw_allowed = os.getenv("ALLOWED_USER_IDS", "").strip()
if _raw_allowed:
    ALLOWED_USER_IDS = [int(x.strip()) for x in _raw_allowed.split(",") if x.strip().isdigit()]
else:
    ALLOWED_USER_IDS = []

# Admin User IDs (Super Admins who can control users & stream servers)
_raw_admin = os.getenv("ADMIN_USER_IDS", "").strip()
if _raw_admin:
    ADMIN_USER_IDS = [int(x.strip()) for x in _raw_admin.split(",") if x.strip().isdigit()]
elif ALLOWED_USER_IDS:
    ADMIN_USER_IDS = [ALLOWED_USER_IDS[0]]
else:
    ADMIN_USER_IDS = []

# Streaming Settings
DEFAULT_LOOP = os.getenv("DEFAULT_LOOP", "true").lower() in ("true", "1", "yes")
AUTO_RECONNECT = os.getenv("AUTO_RECONNECT", "true").lower() in ("true", "1", "yes")
MAX_RECONNECT_ATTEMPTS = int(os.getenv("MAX_RECONNECT_ATTEMPTS", "10"))
RECONNECT_DELAY_SECONDS = int(os.getenv("RECONNECT_DELAY_SECONDS", "5"))

# Disk cleanup threshold: percentage of free disk space before purging old videos
DISK_MIN_FREE_PERCENT = float(os.getenv("DISK_MIN_FREE_PERCENT", "15.0"))

# Watchdog & Auto-Healing Settings
WATCHDOG_CHECK_INTERVAL = int(os.getenv("WATCHDOG_CHECK_INTERVAL", "3"))  # Interval in seconds to inspect FFmpeg health
STALL_TIMEOUT_SECONDS = int(os.getenv("STALL_TIMEOUT_SECONDS", "45"))  # Max seconds without frame progress before declaring freeze
WATCHDOG_MAX_AUTO_HEAL = int(os.getenv("WATCHDOG_MAX_AUTO_HEAL", "50"))  # Max automatic heal restarts per session
RAM_AUTO_CLEAN_PERCENT = float(os.getenv("RAM_AUTO_CLEAN_PERCENT", "88.0"))  # Auto-clean disk cache if RAM exceeds this %
DISK_AUTO_CLEAN_PERCENT = float(os.getenv("DISK_AUTO_CLEAN_PERCENT", "85.0"))  # Auto-clean disk cache if Disk exceeds this %
PLAYLIST_DIR = DATA_DIR / "playlists"
PLAYLIST_DIR.mkdir(parents=True, exist_ok=True)

# Worker Node Settings (for AWS streaming agent)
WORKER_PORT = int(os.getenv("WORKER_PORT", "8000"))
WORKER_HOST = os.getenv("WORKER_HOST", "0.0.0.0")
WORKER_API_KEY = os.getenv("WORKER_API_KEY", "default_secret_key").strip()

# Controller Mode: 'auto' (dispatches to workers if configured, else local), 'remote' (strict worker), 'local'
CONTROLLER_MODE = os.getenv("CONTROLLER_MODE", "auto").lower()
