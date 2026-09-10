"""Конфигурация из окружения / .env."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "да"}


def _log_chat(raw: str):
    raw = (raw or "me").strip()
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw


API_ID: int = _int("API_ID", 0)
API_HASH: str = os.getenv("API_HASH", "").strip()
SESSION: str = os.getenv("SESSION", "").strip()

LOG_CHAT = _log_chat(os.getenv("LOG_CHAT", "me"))
PREFIX: str = (os.getenv("PREFIX", ".") or ".").strip()

DB_PATH: Path = Path(os.getenv("DB_PATH", "data/guard.sqlite3"))
if not DB_PATH.is_absolute():
    DB_PATH = ROOT / DB_PATH

CACHE_TTL_HOURS: int = _int("CACHE_TTL_HOURS", 48)
DELETED_TTL_DAYS: int = _int("DELETED_TTL_DAYS", 30)
MAX_MEDIA_MB: int = _int("MAX_MEDIA_MB", 25)

DEFAULT_ANTIDELETE: bool = _bool("DEFAULT_ANTIDELETE", True)
DEFAULT_LOG_EDITS: bool = _bool("DEFAULT_LOG_EDITS", True)
DEFAULT_SAVE_MEDIA: bool = _bool("DEFAULT_SAVE_MEDIA", True)
ANTIDELETE_GROUPS: bool = _bool("ANTIDELETE_GROUPS", False)
LOG_OWN: bool = _bool("LOG_OWN", False)            # логировать свои удаления/правки

MUTE_ANIM: str = (os.getenv("MUTE_ANIM", "type") or "type").strip().lower()
MUTE_LOG: bool = _bool("MUTE_LOG", True)          # писать перехваченное в лог-чат
ANIM_DELAY: float = _float("ANIM_DELAY", 0.4)

BACKUP_EVERY_MIN: int = _int("BACKUP_EVERY_MIN", 60)
RESTORE_ON_START: bool = _bool("RESTORE_ON_START", True)
BACKUP_TAG = "#guard_backup"

PORT: int = _int("PORT", 8080)



def validate() -> list[str]:
    problems = []
    if not API_ID:
        problems.append("API_ID не задан")
    if not API_HASH:
        problems.append("API_HASH не задан")
    if not SESSION:
        problems.append("SESSION не задан (сгенерируйте: python scripts/gen_session.py)")
    return problems
