from pathlib import Path
import os
import secrets

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("CBV_DATA_DIR", BASE_DIR / "data"))
BUNDLES_DIR = Path(os.environ.get("CBV_BUNDLES_DIR", BASE_DIR / "bundles"))
CACHE_DIR = DATA_DIR / "cache"
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
DB_PATH = DATA_DIR / "dashboard.db"

SHEET_ID = "1ar3gkcE8TpSvbXhxaVV994QIOxmukTXZ2R8KLfQqaLI"
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit"
SHEET_GID = int(os.environ.get("CBV_SHEET_GID", "1420790836"))

AUTH_USERNAME = os.environ.get("CBV_USER", "ACADESINNOVATION")
AUTH_PASSWORD = os.environ.get("CBV_PASS", "INNOVATION2026")

CACHE_TTL = int(os.environ.get("CBV_CACHE_TTL", "300"))

LOW_ENCOUNTER_THRESHOLD = int(os.environ.get("CBV_LOW_ENCOUNTERS", "3"))

for _d in (DATA_DIR, BUNDLES_DIR, CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def get_secret() -> str:
    configured_secret = os.environ.get("CBV_SESSION_SECRET")
    if configured_secret:
        return configured_secret

    key_path = DATA_DIR / "session.key"
    if key_path.exists():
        return key_path.read_text(encoding="utf-8").strip()
    key = secrets.token_hex(32)
    key_path.write_text(key, encoding="utf-8")
    return key
