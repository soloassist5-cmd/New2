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


# ------------------------------------------- «не беспокоить» (gmute) -------
# None — выключен, 0 — бессрочно, иначе метка времени окончания.
dnd_until: int | None = None
dnd_text: str = ""
dnd_since: int = 0
dnd_replied: dict[int, float] = {}      # кому уже ответили, чтобы не долбить

KV_DND_UNTIL, KV_DND_TEXT, KV_DND_SINCE = "dnd_until", "dnd_text", "dnd_since"


async def load_dnd() -> None:
    raw = await db.kv_get(KV_DND_UNTIL, "")
    global dnd_until, dnd_text, dnd_since
    dnd_until = int(raw) if raw not in (None, "") else None
    dnd_text = await db.kv_get(KV_DND_TEXT, "") or ""
    dnd_since = int(await db.kv_get(KV_DND_SINCE, "0") or 0)
    dnd_replied.clear()


async def set_dnd(until: int, text: str) -> None:
    global dnd_until, dnd_text, dnd_since
    dnd_until, dnd_text, dnd_since = until, text, int(time.time())
    dnd_replied.clear()
    await db.kv_set(KV_DND_UNTIL, until)
    await db.kv_set(KV_DND_TEXT, text)
    await db.kv_set(KV_DND_SINCE, dnd_since)


async def clear_dnd() -> None:
    global dnd_until, dnd_text
    dnd_until, dnd_text = None, ""
    dnd_replied.clear()
    await db.kv_set(KV_DND_UNTIL, "")
    await db.kv_set(KV_DND_TEXT, "")


async def dnd_active() -> bool:
    """Проверяет режим и снимает его, когда срок вышел."""
    if dnd_until is None:
        return False
    if dnd_until and dnd_until <= int(time.time()):
        await clear_dnd()
        return False
    return True


# ------------------------------------------------------- белый список ------
allowlist: set[int] = set()


async def load_allowlist() -> None:
    allowlist.clear()
    for row in await db.allowed_users():
        allowlist.add(row["user_id"])


async def allow_user(user_id: int, name: str) -> None:
    allowlist.add(user_id)
    await db.allow(user_id, name)


async def deny_user(user_id: int) -> bool:
    allowlist.discard(user_id)
    return await db.disallow(user_id)


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


def forget_own_deletions() -> None:
    _own_deletions.clear()


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
