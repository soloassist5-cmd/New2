"""Конфигурация из окружения / .env."""
from __future__ import annotations

import hashlib
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

# Куда юзербот складывает копии медиа (после удаления файл уже не скачать).
# Отчёты сюда попадают, только если BOT_TOKEN не задан.
LOG_CHAT = _log_chat(os.getenv("LOG_CHAT", "me"))

# Бот из @BotFather — доставляет отчёты вам в личку.
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
# Кому писать. 0 = сам владелец аккаунта, определяется автоматически.
OWNER_ID: int = _int("OWNER_ID", 0)
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
# Перехваченное всегда пишется в журнал; этот флаг — про мгновенные уведомления.
MUTE_LOG: bool = _bool("MUTE_LOG", False)

# Режим «не беспокоить»: сообщения удаляются, отправитель получает ответ.
DND_TEXT: str = (os.getenv("DND_TEXT", "") or
                 "Я сейчас занят, не принимаю сообщения.").strip()
DND_REPLY_COOLDOWN: int = _int("DND_REPLY_COOLDOWN", 3600)

# Массовое удаление (очистка переписки) — одним файлом вместо сотни карточек.
PURGE_THRESHOLD: int = _int("PURGE_THRESHOLD", 5)
PURGE_DEBOUNCE_SEC: float = _float("PURGE_DEBOUNCE_SEC", 3.0)
PURGE_MEDIA_LIMIT: int = _int("PURGE_MEDIA_LIMIT", 10)
ANIM_DELAY: float = _float("ANIM_DELAY", 0.4)

BACKUP_EVERY_MIN: int = _int("BACKUP_EVERY_MIN", 60)
RESTORE_ON_START: bool = _bool("RESTORE_ON_START", True)
BACKUP_TAG = "#guard_backup"

PORT: int = _int("PORT", 8080)

# Публичный адрес сервиса. Если задан — бот работает через вебхук вместо
# long polling: бесплатные тарифы почти везде именно web service, и держать
# на них постоянный опрос — воевать с площадкой.
# RENDER_EXTERNAL_URL Render подставляет сам — на нём настраивать нечего.
WEBHOOK_URL: str = (
    os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL") or ""
).strip().rstrip("/")
# Telegram шлёт его в заголовке X-Telegram-Bot-Api-Secret-Token.
WEBHOOK_SECRET: str = (os.getenv("WEBHOOK_SECRET", "") or "").strip()


def webhook_secret() -> str:
    """Секрет вебхука. Если не задан — выводим из токена, он и так секретный."""
    if WEBHOOK_SECRET:
        return WEBHOOK_SECRET
    digest = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()
    return digest[:32]


def webhook_path() -> str:
    """Путь считаем от токена, а не от секрета: URL светится в логах и прокси."""
    digest = hashlib.sha256(f"{BOT_TOKEN}:webhook-path".encode()).hexdigest()
    return f"/tg/{digest[:24]}"


def use_webhook() -> bool:
    return bool(BOT_TOKEN and WEBHOOK_URL)



def report_via_bot() -> bool:
    return bool(BOT_TOKEN)


def userbot_enabled() -> bool:
    return bool(SESSION and API_ID and API_HASH)


def validate() -> list[str]:
    """Хотя бы один режим должен быть настроен."""
    problems = []
    if not BOT_TOKEN and not SESSION:
        problems.append(
            "нужен BOT_TOKEN (режим Telegram Business) или SESSION (режим юзербота)")
    if SESSION and not (API_ID and API_HASH):
        problems.append("для SESSION нужны также API_ID и API_HASH с my.telegram.org")
    return problems
