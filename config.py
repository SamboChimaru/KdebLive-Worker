import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Base directories
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DOWNLOAD_DIR = DATA_DIR / "downloads"
CHANNELS_FILE = DATA_DIR / "channels.json"
LOGS_DIR = DATA_DIR / "logs"

# Ensure data directories exist
DATA_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Streaming Settings
DEFAULT_LOOP = os.getenv("DEFAULT_LOOP", "true").lower() in ("true", "1", "yes")
AUTO_RECONNECT = os.getenv("AUTO_RECONNECT", "true").lower() in ("true", "1", "yes")
MAX_RECONNECT_ATTEMPTS = int(os.getenv("MAX_RECONNECT_ATTEMPTS", "10"))
RECONNECT_DELAY_SECONDS = int(os.getenv("RECONNECT_DELAY_SECONDS", "5"))

# Disk cleanup threshold: percentage of free disk space before purging old videos
DISK_MIN_FREE_PERCENT = float(os.getenv("DISK_MIN_FREE_PERCENT", "15.0"))

# Worker Node Settings (for AWS streaming agent)
WORKER_PORT = int(os.getenv("WORKER_PORT", "8000"))
WORKER_HOST = os.getenv("WORKER_HOST", "0.0.0.0")
WORKER_API_KEY = os.getenv("WORKER_API_KEY", "default_secret_key").strip()
