"""Разделяемое состояние процесса: клиент, лог-чат, муты, AFK."""
from __future__ import annotations

import time
from collections import OrderedDict

import db

client = None            # TelegramClient юзербота, выставляется в main.py
me = None                # telethon User — владелец аккаунта
log_entity = None        # чат-хранилище копий медиа

api = None               # bot.api.BotAPI — бот из @BotFather (может отсутствовать)
bot_user: dict | None = None      # результат getMe
owner_id: int = 0        # владелец: кому уходят отчёты
owner_chat_id: int = 0   # его личка с ботом
bot_blocked: bool = False  # бот не может писать владельцу — предупреждаем один раз

# Подключения Telegram Business: connection_id -> {user_id, rights, is_enabled}
business: dict[str, dict] = {}


def business_for(user_id: int) -> str | None:
    """Активное бизнес-подключение владельца, если оно есть."""
    for connection_id, info in business.items():
        if info.get("user_id") == user_id and info.get("is_enabled"):
            return connection_id
    return None


def rights_of(connection_id: str | None) -> dict:
    return (business.get(connection_id) or {}).get("rights") or {}

start_time: float = time.time()

# ---------------------------------------------------------------- AFK ------
afk_since: float | None = None
afk_reason: str = ""
afk_replied: dict[int, float] = {}


def set_afk(reason: str) -> None:
    global afk_since, afk_reason
    afk_since, afk_reason = time.time(), reason
    afk_replied.clear()


def clear_afk() -> None:
    global afk_since, afk_reason
    afk_since, afk_reason = None, ""
    afk_replied.clear()


# ------------------------------------------------------- удалено нами ------
# Чтобы антиудаление не логировало то, что удалил сам бот (.mute/.del/.purge).
_own_deletions: OrderedDict[tuple[int, int], float] = OrderedDict()
_OWN_LIMIT = 4000


def mark_own_deletion(chat_id: int, *msg_ids: int, private: bool = False) -> None:
    """private=True добавляет алиас без chat_id: в личках его нет в событии удаления.

    В группах алиас не ставим — там id сообщений маленькие и пересекаются между чатами.
    """
    for mid in msg_ids:
        _own_deletions[(chat_id, mid)] = time.time()
        if private:
            _own_deletions[(0, mid)] = time.time()
    while len(_own_deletions) > _OWN_LIMIT:
        _own_deletions.popitem(last=False)


def was_own_deletion(chat_id: int | None, msg_id: int) -> bool:
    key = (chat_id if chat_id is not None else 0, msg_id)
    if key in _own_deletions:
        return True
    return (0, msg_id) in _own_deletions


# ------------------------------------------------------------- муты --------
# (chat_id, user_id) -> until (0 = бессрочно). chat_id 0 = глобальный мут.
mutes: dict[tuple[int, int], int] = {}


async def load_mutes() -> None:
    mutes.clear()
    for row in await db.all_mutes():
        mutes[(row["chat_id"], row["user_id"])] = row["until"]


async def mute_user(chat_id: int, user_id: int, until: int, reason: str = "") -> None:
    mutes[(chat_id, user_id)] = until
    await db.add_mute(chat_id, user_id, until, reason)


async def unmute_user(chat_id: int, user_id: int) -> bool:
    existed = mutes.pop((chat_id, user_id), None) is not None
    await db.remove_mute(chat_id, user_id)
    return existed


async def is_muted(chat_id: int, user_id: int) -> bool:
    """Проверяет локальный и глобальный мут, снимая протухшие."""
    now = int(time.time())
    for key in ((chat_id, user_id), (0, user_id)):
        until = mutes.get(key)
        if until is None:
            continue
        if until and until <= now:
            await unmute_user(key[0], user_id)
            continue
        return True
    return False
