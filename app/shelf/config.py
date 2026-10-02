import os
from pathlib import Path

from dotenv import load_dotenv

# Local runs read the project's .env; Docker Compose injects these variables.
# Existing environment variables take precedence; never store credentials in SQLite.
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)

DATA = Path(os.environ.get("DATA_DIR", "/data"))
INBOX = DATA / "inbox"          # drop PKGs / split parts / archives / dump folders here
LIBRARY = DATA / "library"      # validated, installable PKGs: <TITLE_ID>/<CONTENT_ID>.pkg
WORK = DATA / "work"            # downloads in progress and conversion scratch
FAILED = DATA / "failed"        # inputs that could not be converted (kept for inspection)
ICONS = DATA / "icons"
DB_PATH = DATA / "shelf.db"
PKGTOOL = os.environ.get("PKGTOOL", "/opt/pkgtool/PkgTool.Core")
CATALOG_PATH = os.environ.get("CATALOG_PATH", "/app/catalog/GAMES.json")
ARIA2_CONN = int(os.environ.get("ARIA2_CONN", "8"))
# Abort a download that writes no new bytes for this long (dead/slow archive.org node).
STALL_SECONDS = int(os.environ.get("STALL_SECONDS", "180"))
# aria2 closes a connection whose speed drops below this (dead-node crawls). Suffix K/M allowed.
MIN_DOWNLOAD_RATE = os.environ.get("MIN_DOWNLOAD_RATE", "100K")

# Host allow-list for user-supplied download URLs (SSRF guard); a leading dot also matches subdomains.
ALLOWED_DOWNLOAD_HOSTS = tuple(
    h.strip().lower().rstrip(".") for h in os.environ.get("ALLOWED_DOWNLOAD_HOSTS", "archive.org,.archive.org").split(",") if h.strip()
)
ALLOW_ANY_DOWNLOAD = os.environ.get("ALLOW_ANY_DOWNLOAD", "0").strip().lower() in ("1", "true", "yes", "on")


def _env_gb(name, default):
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return float(default)


MAX_UPLOAD_GB = _env_gb("MAX_UPLOAD_GB", "200")
MAX_UPLOAD_BYTES = int(MAX_UPLOAD_GB * 2**30)
EXTRACT_MAX_GB = _env_gb("EXTRACT_MAX_GB", "300")
EXTRACT_MAX_BYTES = int(EXTRACT_MAX_GB * 2**30)

# Defaults for settings editable in the UI (stored in the DB once saved).
DEFAULTS = {
    "ps4_host": os.environ.get("PS4_HOST", ""),
    "ftp_port": os.environ.get("PS4_FTP_PORT", "2121"),
    "ftp_dir": os.environ.get("PS4_FTP_DIR", "/data/pkg"),
    "ftp_user": os.environ.get("PS4_FTP_USER", "anonymous"),
    "ftp_pass": os.environ.get("PS4_FTP_PASS", ""),
    "rpi_port": os.environ.get("PS4_RPI_PORT", "12800"),
    # URL the PS4 uses to reach this server (LAN IP, not the tailnet one).
    "public_url": os.environ.get("PUBLIC_URL", ""),
    # none | ftp | rpi — what to do automatically once a PKG lands in the library.
    "auto_send": os.environ.get("AUTO_SEND", "none"),
}

for d in (INBOX, LIBRARY, WORK, FAILED, ICONS):
    d.mkdir(parents=True, exist_ok=True)
