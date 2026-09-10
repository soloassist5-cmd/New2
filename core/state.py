"""Разделяемое состояние процесса.

Бот многопользовательский: у каждого владельца свой набор мутов, белый список
и режим «не беспокоить». Администратор (OWNER_ID) — тот, кто развернул бота:
он решает, кого пускать, и получает служебные уведомления.
"""
from __future__ import annotations

import time
from collections import OrderedDict

import config
import db

client = None            # TelegramClient юзербота, выставляется в main.py
me = None                # telethon User — владелец аккаунта в режиме юзербота
log_entity = None        # чат-хранилище копий медиа (только для юзербота)

api = None               # bot.api.BotAPI — бот из @BotFather
bot_user: dict | None = None      # результат getMe
bot_blocked: bool = False         # бот не может писать — предупреждаем один раз

start_time: float = time.time()

# Подключения Telegram Business: connection_id -> {user_id, rights, is_enabled}
business: dict[str, dict] = {}

# Кэш таблицы users: user_id -> {status, chat_id, dnd_since, name}
users: dict[int, dict] = {}


def admin_id() -> int:
    return config.OWNER_ID


def userbot_owner() -> int:
    """В режиме юзербота владелец один — сам аккаунт."""
    return me.id if me is not None else 0


def is_admin(user_id: int) -> bool:
    return bool(config.OWNER_ID) and user_id == config.OWNER_ID


def is_approved(user_id: int) -> bool:
    if is_admin(user_id):
        return True
    return (users.get(user_id) or {}).get("status") == db.APPROVED


def status_of(user_id: int) -> str:
    if is_admin(user_id):
        return db.APPROVED
    return (users.get(user_id) or {}).get("status") or ""


def chat_of(user_id: int) -> int:
    """Личка владельца с ботом — куда ему приходят отчёты."""
    chat_id = (users.get(user_id) or {}).get("chat_id")
    return chat_id or user_id


def owners() -> list[int]:
    return [uid for uid, row in users.items() if row.get("status") == db.APPROVED]


async def load_users() -> None:
    users.clear()
    for row in await db.all_users():
        users[row["user_id"]] = {
            "name": row["name"], "status": row["status"],
            "chat_id": row["chat_id"], "dnd_since": row["dnd_since"],
        }
    if config.OWNER_ID and config.OWNER_ID not in users:
        users[config.OWNER_ID] = {"name": None, "status": db.APPROVED,
                                  "chat_id": config.OWNER_ID, "dnd_since": 0}


async def remember_user(user_id: int, *, name: str | None = None,
                        chat_id: int | None = None,
                        status: str | None = None) -> dict:
    row = await db.upsert_user(user_id, name=name, chat_id=chat_id, status=status)
    users[user_id] = {"name": row["name"], "status": row["status"],
                      "chat_id": row["chat_id"], "dnd_since": row["dnd_since"]}
    return users[user_id]


def business_of(user_id: int) -> str | None:
    """Активное бизнес-подключение владельца, если оно есть."""
    for connection_id, info in business.items():
        if info.get("user_id") == user_id and info.get("is_enabled"):
            return connection_id
    return None


def owner_of(connection_id: str | None) -> int:
    return (business.get(connection_id) or {}).get("user_id", 0)


def rights_of(connection_id: str | None) -> dict:
    return (business.get(connection_id) or {}).get("rights") or {}


# ------------------------------------------- «не беспокоить» (gmute) -------
# Режим включается и выключается только вручную: срок здесь скорее вредит —
# забыть выключить проще, чем не заметить, что он ещё включён.
dnd_replied: dict[tuple[int, int], float] = {}     # (владелец, кто) -> когда


def dnd_active(owner_id: int) -> bool:
    return bool((users.get(owner_id) or {}).get("dnd_since"))


def dnd_since(owner_id: int) -> int:
    return (users.get(owner_id) or {}).get("dnd_since") or 0


async def set_dnd(owner_id: int) -> None:
    since = int(time.time())
    users.setdefault(owner_id, {"status": db.APPROVED, "chat_id": owner_id})
    users[owner_id]["dnd_since"] = since
    _forget_dnd_replies(owner_id)
    await db.set_dnd_since(owner_id, since)


async def clear_dnd(owner_id: int) -> None:
    if owner_id in users:
        users[owner_id]["dnd_since"] = 0
    _forget_dnd_replies(owner_id)
    await db.set_dnd_since(owner_id, 0)


def _forget_dnd_replies(owner_id: int) -> None:
    for key in [k for k in dnd_replied if k[0] == owner_id]:
        dnd_replied.pop(key, None)


# ------------------------------------------------------- белый список ------
allowlist: dict[int, set[int]] = {}


async def load_allowlist() -> None:
    allowlist.clear()
    for row in await db.allowed_users():
        allowlist.setdefault(row["owner_id"], set()).add(row["user_id"])


def is_allowed(owner_id: int, user_id: int) -> bool:
    return user_id in allowlist.get(owner_id, ())


async def allow_user(owner_id: int, user_id: int, name: str) -> None:
    allowlist.setdefault(owner_id, set()).add(user_id)
    await db.allow(owner_id, user_id, name)


async def deny_user(owner_id: int, user_id: int) -> bool:
    allowlist.get(owner_id, set()).discard(user_id)
    return await db.disallow(owner_id, user_id)


# ------------------------------------------------------- удалено нами ------
# Чтобы антиудаление не логировало то, что удалил сам бот (.mute/.del/.purge).
_own_deletions: OrderedDict[tuple[int, int, int], float] = OrderedDict()
_OWN_LIMIT = 4000


def mark_own_deletion(owner_id: int, chat_id: int, *msg_ids: int,
                      private: bool = False) -> None:
    """private=True добавляет алиас без chat_id: у юзербота его нет в событии.

    В остальных случаях алиас не ставим — id сообщений пересекаются между чатами.
    """
    for mid in msg_ids:
        _own_deletions[(owner_id, chat_id, mid)] = time.time()
        if private:
            _own_deletions[(owner_id, 0, mid)] = time.time()
    while len(_own_deletions) > _OWN_LIMIT:
        _own_deletions.popitem(last=False)


def forget_own_deletions() -> None:
    _own_deletions.clear()


def was_own_deletion(owner_id: int, chat_id: int | None, msg_id: int) -> bool:
    if (owner_id, chat_id if chat_id is not None else 0, msg_id) in _own_deletions:
        return True
    return (owner_id, 0, msg_id) in _own_deletions


# ------------------------------------------------------------- муты --------
# (owner_id, chat_id, user_id) -> until. chat_id 0 = во всех чатах владельца.
mutes: dict[tuple[int, int, int], int] = {}


async def load_mutes() -> None:
    mutes.clear()
    for row in await db.all_mutes():
        mutes[(row["owner_id"], row["chat_id"], row["user_id"])] = row["until"]


async def mute_user(owner_id: int, chat_id: int, user_id: int, until: int,
                    reason: str = "") -> None:
    mutes[(owner_id, chat_id, user_id)] = until
    await db.add_mute(owner_id, chat_id, user_id, until, reason)


async def unmute_user(owner_id: int, chat_id: int, user_id: int) -> bool:
    existed = mutes.pop((owner_id, chat_id, user_id), None) is not None
    await db.remove_mute(owner_id, chat_id, user_id)
    return existed


async def is_muted(owner_id: int, chat_id: int, user_id: int) -> bool:
    """Проверяет мут в чате и во всех чатах владельца, снимая протухшие."""
    now = int(time.time())
    for scope in (chat_id, 0):
        until = mutes.get((owner_id, scope, user_id))
        if until is None:
            continue
        if until and until <= now:
            await unmute_user(owner_id, scope, user_id)
            continue
        return True
    return False


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
