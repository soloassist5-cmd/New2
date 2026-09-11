"""Слой доступа к SQLite (aiosqlite). Один процесс — одно соединение.

Бот многопользовательский: у каждого владельца свои чаты, муты и журнал,
поэтому owner_id входит в ключ почти каждой таблицы. Без этого пользователи
видели бы переписку друг друга — id личного чата в Bot API совпадает с id
собеседника и одинаков для всех владельцев.
"""
from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

import aiosqlite

import config

_conn: aiosqlite.Connection | None = None

SCHEMA_VERSION = 2

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

-- Люди, которым разрешено пользоваться ботом, плюс их персональные флаги.
CREATE TABLE IF NOT EXISTS users (
    user_id      INTEGER PRIMARY KEY,
    name         TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | denied
    chat_id      INTEGER,                           -- личка с ботом
    dnd_since    INTEGER NOT NULL DEFAULT 0,        -- 0 = «не беспокоить» выключен
    requested_at INTEGER NOT NULL,
    decided_at   INTEGER
);

CREATE TABLE IF NOT EXISTS messages (
    owner_id   INTEGER NOT NULL,
    chat_id    INTEGER NOT NULL,
    msg_id     INTEGER NOT NULL,
    user_id    INTEGER,
    is_private INTEGER NOT NULL DEFAULT 0,
    text       TEXT,
    media_type TEXT,
    media_ref  INTEGER,
    file_id    TEXT,
    business_id TEXT,
    user_name  TEXT,
    reply_to   INTEGER,
    date       INTEGER NOT NULL,
    PRIMARY KEY (owner_id, chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_msg_id   ON messages(owner_id, msg_id);
CREATE INDEX IF NOT EXISTS idx_msg_date ON messages(date);

CREATE TABLE IF NOT EXISTS deleted (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   INTEGER NOT NULL,
    chat_id    INTEGER,
    msg_id     INTEGER,
    user_id    INTEGER,
    user_name  TEXT,
    text       TEXT,
    media_type TEXT,
    media_ref  INTEGER,
    file_id    TEXT,
    date       INTEGER,
    deleted_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_del_chat ON deleted(owner_id, chat_id, deleted_at);

CREATE TABLE IF NOT EXISTS edits (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id  INTEGER NOT NULL,
    chat_id   INTEGER,
    msg_id    INTEGER,
    user_id   INTEGER,
    old_text  TEXT,
    new_text  TEXT,
    edited_at INTEGER NOT NULL
);

-- Что бот удалил за владельца: мут конкретного человека или «не беспокоить».
CREATE TABLE IF NOT EXISTS intercepted (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   INTEGER NOT NULL,
    chat_id    INTEGER,
    msg_id     INTEGER,
    user_id    INTEGER,
    user_name  TEXT,
    text       TEXT,
    media_type TEXT,
    file_id    TEXT,
    date       INTEGER,
    at         INTEGER NOT NULL,
    reason     TEXT NOT NULL DEFAULT 'mute'
);
CREATE INDEX IF NOT EXISTS idx_intercepted ON intercepted(owner_id, user_id, at);

CREATE TABLE IF NOT EXISTS mutes (
    owner_id   INTEGER NOT NULL,
    chat_id    INTEGER NOT NULL,   -- 0 = во всех чатах владельца
    user_id    INTEGER NOT NULL,
    until      INTEGER NOT NULL DEFAULT 0,
    reason     TEXT,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (owner_id, chat_id, user_id)
);

-- Срочные вызовы: кто и когда дёргал владельца сквозь «не беспокоить».
CREATE TABLE IF NOT EXISTS urgent_calls (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    user_name TEXT,
    chat_id   INTEGER,
    reason    TEXT,
    at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_urgent ON urgent_calls(owner_id, user_id, at);

-- Кого «не беспокоить» пропускает.
CREATE TABLE IF NOT EXISTS allowlist (
    owner_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    name     TEXT,
    added_at INTEGER NOT NULL,
    PRIMARY KEY (owner_id, user_id)
);

-- Как человека звали, когда бот видел его в прошлый раз. Только собственные
-- наблюдения: наружу за историей имён бот не ходит.
CREATE TABLE IF NOT EXISTS aliases (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    name       TEXT,
    username   TEXT,
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alias ON aliases(owner_id, user_id, first_seen);

CREATE TABLE IF NOT EXISTS settings (
    owner_id   INTEGER NOT NULL,
    chat_id    INTEGER NOT NULL,
    title      TEXT,
    antidelete INTEGER,
    log_edits  INTEGER,
    save_media INTEGER,
    ignored    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (owner_id, chat_id)
);

CREATE TABLE IF NOT EXISTS notes (
    owner_id INTEGER NOT NULL,
    chat_id  INTEGER NOT NULL,
    name     TEXT NOT NULL,
    content  TEXT,
    PRIMARY KEY (owner_id, chat_id, name)
);

CREATE TABLE IF NOT EXISTS business (
    connection_id TEXT PRIMARY KEY,
    user_id       INTEGER NOT NULL,
    user_chat_id  INTEGER,
    is_enabled    INTEGER NOT NULL DEFAULT 1,
    rights        TEXT,
    connected_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""

# Колонки, добавленные после выхода схемы: досоздаются на месте.
LATE_COLUMNS = {"settings": {"title": "TEXT"}}

# Таблицы, которые в первой версии схемы жили без owner_id.
V1_TABLES = ("messages", "deleted", "edits", "intercepted", "mutes",
             "allowlist", "settings", "notes")


def now() -> int:
    return int(time.time())


async def init() -> None:
    global _conn
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = await aiosqlite.connect(config.DB_PATH)
    connection.row_factory = aiosqlite.Row
    try:
        _conn = connection
        await _migrate_to_v2(connection)
        await connection.executescript(SCHEMA)
        await _add_late_columns(connection)
        await connection.commit()
    except Exception:
        # Иначе останется висеть рабочий поток aiosqlite и битое соединение.
        _conn = None
        await connection.close()
        raise


async def _add_late_columns(connection: aiosqlite.Connection) -> None:
    for table, columns in LATE_COLUMNS.items():
        async with connection.execute(f"PRAGMA table_info({table})") as cur:
            existing = {row[1] for row in await cur.fetchall()}
        for name, sql_type in columns.items():
            if name not in existing:
                await connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


async def _migrate_to_v2(connection: aiosqlite.Connection) -> None:
    """Переносит данные одного владельца в многопользовательскую схему."""
    async with connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'") as cur:
        tables = {row[0] for row in await cur.fetchall()}
    if "messages" not in tables:
        return                                   # свежая база — мигрировать нечего
    async with connection.execute("PRAGMA table_info(messages)") as cur:
        columns = {row[1] for row in await cur.fetchall()}
    if "owner_id" in columns:
        return                                   # уже v2

    owner = config.OWNER_ID
    if not owner and "business" in tables:
        async with connection.execute(
                "SELECT user_id FROM business LIMIT 1") as cur:
            row = await cur.fetchone()
            owner = row[0] if row else 0

    for table in V1_TABLES:
        if table in tables:
            await connection.execute(f"ALTER TABLE {table} RENAME TO {table}_v1")
    await connection.executescript(SCHEMA)

    for table in V1_TABLES:
        if f"{table}_v1" not in {f"{t}_v1" for t in tables if t in V1_TABLES}:
            continue
        async with connection.execute(f"PRAGMA table_info({table}_v1)") as cur:
            old_columns = [row[1] for row in await cur.fetchall()]
        async with connection.execute(f"PRAGMA table_info({table})") as cur:
            new_columns = {row[1] for row in await cur.fetchall()}
        shared = [name for name in old_columns if name in new_columns]
        fields = ", ".join(shared)
        await connection.execute(
            f"INSERT OR IGNORE INTO {table}(owner_id, {fields}) "
            f"SELECT ?, {fields} FROM {table}_v1", (owner,))
        await connection.execute(f"DROP TABLE {table}_v1")

    # «Не беспокоить» переехал из kv в users.
    async with connection.execute("SELECT v FROM kv WHERE k='dnd_on'") as cur:
        row = await cur.fetchone()
    if row and row[0] and owner:
        async with connection.execute("SELECT v FROM kv WHERE k='dnd_since'") as cur:
            since_row = await cur.fetchone()
        since = int(since_row[0]) if since_row and since_row[0] else now()
        await connection.execute(
            "INSERT INTO users(user_id,status,dnd_since,requested_at) "
            "VALUES (?,'approved',?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET dnd_since=excluded.dnd_since",
            (owner, since, now()))
    await connection.commit()


async def is_valid_database(path) -> bool:
    """Проверяет, что файл — целая база Guard, а не что-то другое."""
    try:
        async with aiosqlite.connect(path) as probe:
            async with probe.execute("PRAGMA integrity_check") as cur:
                row = await cur.fetchone()
            if not row or row[0] != "ok":
                return False
            async with probe.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name IN ('messages','mutes')") as cur:
                return (await cur.fetchone())[0] == 2
    except Exception:                                        # noqa: BLE001
        return False


async def close() -> None:
    global _conn
    if _conn is not None:
        await _conn.commit()
        await _conn.close()
        _conn = None


def conn() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("БД не инициализирована")
    return _conn


async def execute(sql: str, params: Iterable[Any] = ()) -> None:
    await conn().execute(sql, tuple(params))
    await conn().commit()


async def fetchone(sql: str, params: Iterable[Any] = ()):
    async with conn().execute(sql, tuple(params)) as cur:
        return await cur.fetchone()


async def fetchall(sql: str, params: Iterable[Any] = ()):
    async with conn().execute(sql, tuple(params)) as cur:
        return await cur.fetchall()


async def scalar(sql: str, params: Iterable[Any] = (), default=0):
    row = await fetchone(sql, params)
    if row is None or row[0] is None:
        return default
    return row[0]


# ------------------------------------------------------------ пользователи --

APPROVED, PENDING, DENIED = "approved", "pending", "denied"


async def upsert_user(user_id: int, *, name: str | None = None,
                      chat_id: int | None = None,
                      status: str | None = None) -> dict:
    await execute(
        """INSERT INTO users(user_id,name,chat_id,status,requested_at)
           VALUES (?,?,?,COALESCE(?, 'pending'),?)
           ON CONFLICT(user_id) DO UPDATE SET
                name=COALESCE(excluded.name, users.name),
                chat_id=COALESCE(excluded.chat_id, users.chat_id),
                status=COALESCE(?, users.status),
                decided_at=CASE WHEN ? IS NULL THEN users.decided_at ELSE ? END""",
        (user_id, name, chat_id, status, now(), status, status, now()))
    return await get_user(user_id)


async def get_user(user_id: int):
    return await fetchone("SELECT * FROM users WHERE user_id=?", (user_id,))


async def users_by_status(status: str):
    return await fetchall("SELECT * FROM users WHERE status=? ORDER BY requested_at",
                          (status,))


async def all_users():
    return await fetchall("SELECT * FROM users ORDER BY requested_at")


async def set_dnd_since(user_id: int, since: int) -> None:
    await execute(
        "INSERT INTO users(user_id,status,dnd_since,requested_at) "
        "VALUES (?,'approved',?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET dnd_since=excluded.dnd_since",
        (user_id, since, now()))


# ---------------------------------------------------------------- messages ---

async def cache_message(owner_id: int, chat_id: int, msg_id: int,
                        user_id: int | None, is_private: bool, text: str | None,
                        media_type: str | None, media_ref: int | None,
                        reply_to: int | None, date: int,
                        file_id: str | None = None, business_id: str | None = None,
                        user_name: str | None = None) -> None:
    await execute(
        """INSERT INTO messages(owner_id,chat_id,msg_id,user_id,is_private,text,
                                media_type,media_ref,reply_to,date,file_id,
                                business_id,user_name)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(owner_id,chat_id,msg_id) DO UPDATE SET
                text=excluded.text,
                media_type=excluded.media_type,
                media_ref=COALESCE(excluded.media_ref, messages.media_ref),
                file_id=COALESCE(excluded.file_id, messages.file_id),
                business_id=COALESCE(excluded.business_id, messages.business_id),
                user_name=COALESCE(excluded.user_name, messages.user_name)""",
        (owner_id, chat_id, msg_id, user_id, int(is_private), text, media_type,
         media_ref, reply_to, date, file_id, business_id, user_name))


async def set_media_ref(owner_id: int, chat_id: int, msg_id: int, ref: int) -> None:
    await execute("UPDATE messages SET media_ref=? "
                  "WHERE owner_id=? AND chat_id=? AND msg_id=?",
                  (ref, owner_id, chat_id, msg_id))


async def get_message(owner_id: int, chat_id: int, msg_id: int):
    return await fetchone(
        "SELECT * FROM messages WHERE owner_id=? AND chat_id=? AND msg_id=?",
        (owner_id, chat_id, msg_id))


async def get_messages(owner_id: int, chat_id: int, msg_ids: list[int]):
    """Сообщения чата по списку id, по порядку. SQLite ограничивает число
    подстановок в запросе, поэтому идём частями."""
    found = []
    for start in range(0, len(msg_ids), 400):
        chunk = msg_ids[start:start + 400]
        placeholders = ",".join("?" * len(chunk))
        found += await fetchall(
            f"SELECT * FROM messages WHERE owner_id=? AND chat_id=? "
            f"AND msg_id IN ({placeholders})", (owner_id, chat_id, *chunk))
    return sorted(found, key=lambda row: (row["date"], row["msg_id"]))


async def find_private_message(owner_id: int, msg_id: int):
    """У юзербота событие удаления в личке не содержит chat_id."""
    return await fetchone(
        "SELECT * FROM messages WHERE owner_id=? AND msg_id=? AND is_private=1 "
        "ORDER BY date DESC LIMIT 1", (owner_id, msg_id))


async def drop_message(owner_id: int, chat_id: int, msg_id: int) -> None:
    await execute("DELETE FROM messages WHERE owner_id=? AND chat_id=? AND msg_id=?",
                  (owner_id, chat_id, msg_id))


async def drop_messages(owner_id: int, chat_id: int, msg_ids: list[int]) -> None:
    for start in range(0, len(msg_ids), 400):
        chunk = msg_ids[start:start + 400]
        placeholders = ",".join("?" * len(chunk))
        await conn().execute(
            f"DELETE FROM messages WHERE owner_id=? AND chat_id=? "
            f"AND msg_id IN ({placeholders})", (owner_id, chat_id, *chunk))
    await conn().commit()


# ----------------------------------------------------------------- deleted ---

async def add_deleted(row: dict) -> int:
    cur = await conn().execute(
        """INSERT INTO deleted(owner_id,chat_id,msg_id,user_id,user_name,text,
                               media_type,media_ref,file_id,date,deleted_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (row.get("owner_id"), row.get("chat_id"), row.get("msg_id"),
         row.get("user_id"), row.get("user_name"), row.get("text"),
         row.get("media_type"), row.get("media_ref"), row.get("file_id"),
         row.get("date"), now()))
    await conn().commit()
    return cur.lastrowid


async def last_deleted(owner_id: int, chat_id: int | None, limit: int = 10):
    if chat_id is None:
        return await fetchall(
            "SELECT * FROM deleted WHERE owner_id=? ORDER BY deleted_at DESC LIMIT ?",
            (owner_id, limit))
    return await fetchall(
        "SELECT * FROM deleted WHERE owner_id=? AND chat_id=? "
        "ORDER BY deleted_at DESC LIMIT ?", (owner_id, chat_id, limit))


async def add_edit(owner_id: int, chat_id, msg_id, user_id, old_text,
                   new_text) -> None:
    await execute(
        "INSERT INTO edits(owner_id,chat_id,msg_id,user_id,old_text,new_text,"
        "edited_at) VALUES (?,?,?,?,?,?,?)",
        (owner_id, chat_id, msg_id, user_id, old_text, new_text, now()))


# ------------------------------------------------------------- перехваты ----

async def add_intercepted(row: dict, reason: str = "mute") -> None:
    await execute(
        """INSERT INTO intercepted(owner_id,chat_id,msg_id,user_id,user_name,text,
                                   media_type,file_id,date,at,reason)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (row.get("owner_id"), row.get("chat_id"), row.get("msg_id"),
         row.get("user_id"), row.get("user_name"), row.get("text"),
         row.get("media_type"), row.get("file_id"), row.get("date"), now(), reason))


async def intercepted(owner_id: int, user_id: int | None = None, *, since: int = 0,
                      reason: str | None = None, limit: int = 50):
    where, params = ["owner_id = ?", "at >= ?"], [owner_id, since]
    if user_id is not None:
        where.append("user_id = ?")
        params.append(user_id)
    if reason is not None:
        where.append("reason = ?")
        params.append(reason)
    params.append(limit)
    return await fetchall(
        f"SELECT * FROM intercepted WHERE {' AND '.join(where)} "
        f"ORDER BY at DESC LIMIT ?", params)


# ------------------------------------------------------------------- mutes ---

async def count_intercepted(owner_id: int, *, since: int = 0) -> int:
    return await scalar(
        "SELECT COUNT(*) FROM intercepted WHERE owner_id=? AND at >= ?",
        (owner_id, since))


def _intercepted_scope(owner_id: int, chat_id: int | None,
                       before: int | None) -> tuple[str, list]:
    where, params = ["owner_id = ?"], [owner_id]
    if chat_id is not None:
        where.append("chat_id = ?")
        params.append(chat_id)
    if before is not None:
        where.append("at < ?")
        params.append(before)
    return " AND ".join(where), params


async def intercepted_in_scope(owner_id: int, *, chat_id: int | None = None,
                               before: int | None = None) -> int:
    """Сколько попадёт под чистку — показываем до того, как удалять."""
    where, params = _intercepted_scope(owner_id, chat_id, before)
    return await scalar(f"SELECT COUNT(*) FROM intercepted WHERE {where}", params)


async def clear_intercepted(owner_id: int, *, chat_id: int | None = None,
                            before: int | None = None) -> int:
    """Чистит журнал перехваченного. Возвращает, сколько удалено."""
    where, params = _intercepted_scope(owner_id, chat_id, before)
    cur = await conn().execute(f"DELETE FROM intercepted WHERE {where}", params)
    await conn().commit()
    return cur.rowcount or 0


# Как звали человека в прошлый раз — чтобы не ходить в базу на каждое сообщение.
_alias_cache: dict[tuple[int, int], tuple[str | None, str | None]] = {}


def forget_aliases() -> None:
    _alias_cache.clear()


async def note_alias(owner_id: int, user_id: int, name: str | None,
                     username: str | None) -> bool:
    """Запоминает имя, если оно изменилось. True — было переименование."""
    if not user_id or not (name or username):
        return False
    key = (owner_id, user_id)
    if _alias_cache.get(key) == (name, username):
        return False

    last = await fetchone(
        "SELECT * FROM aliases WHERE owner_id=? AND user_id=? "
        "ORDER BY first_seen DESC, id DESC LIMIT 1", (owner_id, user_id))
    moment = now()
    if last is not None and (last["name"], last["username"]) == (name, username):
        _alias_cache[key] = (name, username)
        return False

    if last is not None:
        await execute("UPDATE aliases SET last_seen=? WHERE id=?",
                      (moment, last["id"]))
    await execute(
        "INSERT INTO aliases(owner_id,user_id,name,username,first_seen,last_seen) "
        "VALUES (?,?,?,?,?,?)", (owner_id, user_id, name, username, moment, moment))
    _alias_cache[key] = (name, username)
    return last is not None


async def aliases(owner_id: int, user_id: int, limit: int = 10):
    return await fetchall(
        "SELECT * FROM aliases WHERE owner_id=? AND user_id=? "
        "ORDER BY first_seen DESC, id DESC LIMIT ?", (owner_id, user_id, limit))


async def message_times(owner_id: int, user_id: int, limit: int = 5000):
    """Когда человек писал — для портрета активности. Только даты, без текста."""
    rows = await fetchall(
        "SELECT date FROM messages WHERE owner_id=? AND user_id=? "
        "ORDER BY date LIMIT ?", (owner_id, user_id, limit))
    return [row["date"] for row in rows]


async def dossier(owner_id: int, user_id: int, chat_id: int | None = None) -> dict:
    """Всё, что бот успел записать про человека. Только своя база, без разведки."""
    msgs = await fetchone(
        "SELECT COUNT(*) AS total, MIN(date) AS first, MAX(date) AS last, "
        "SUM(media_type IS NOT NULL) AS media FROM messages "
        "WHERE owner_id=? AND user_id=?", (owner_id, user_id))
    dels = await fetchone(
        "SELECT COUNT(*) AS total, MAX(deleted_at) AS last FROM deleted "
        "WHERE owner_id=? AND user_id=?", (owner_id, user_id))
    edits_row = await fetchone(
        "SELECT COUNT(*) AS total, MAX(edited_at) AS last FROM edits "
        "WHERE owner_id=? AND user_id=?", (owner_id, user_id))
    calls = await fetchone(
        "SELECT COUNT(*) AS total, MAX(at) AS last FROM urgent_calls "
        "WHERE owner_id=? AND user_id=?", (owner_id, user_id))
    kept = await fetchall(
        "SELECT reason, COUNT(*) AS total FROM intercepted "
        "WHERE owner_id=? AND user_id=? GROUP BY reason", (owner_id, user_id))

    def counted(row, extra=()):
        out = {"total": (row["total"] if row else 0) or 0,
               "last": row["last"] if row else None}
        for key in extra:
            out[key] = (row[key] if row else 0) or 0
        return out

    return {
        "user_id": user_id,
        "name": await name_of(owner_id, user_id),
        "messages": counted(msgs, ("media",)) | {"first": msgs["first"] if msgs
                                                 else None},
        "deleted": counted(dels),
        "edits": counted(edits_row),
        "urgent": counted(calls),
        "intercepted": {row["reason"]: row["total"] for row in kept},
        "allowed": await fetchone(
            "SELECT * FROM allowlist WHERE owner_id=? AND user_id=?",
            (owner_id, user_id)) is not None,
        "mutes": await fetchall(
            "SELECT * FROM mutes WHERE owner_id=? AND user_id=?",
            (owner_id, user_id)),
        "chat": await fetchone(
            "SELECT * FROM settings WHERE owner_id=? AND chat_id=?",
            (owner_id, chat_id)) if chat_id is not None else None,
    }


async def intercepted_summary(owner_id: int) -> dict:
    """Итог по журналу: сколько, из скольких чатов и с какого времени."""
    row = await fetchone(
        "SELECT COUNT(*) AS total, COUNT(DISTINCT chat_id) AS chats, "
        "MIN(at) AS oldest, MAX(at) AS newest "
        "FROM intercepted WHERE owner_id=?", (owner_id,))
    if row is None:
        return {"total": 0, "chats": 0, "oldest": None, "newest": None}
    return {"total": row["total"] or 0, "chats": row["chats"] or 0,
            "oldest": row["oldest"], "newest": row["newest"]}


async def add_mute(owner_id: int, chat_id: int, user_id: int, until: int,
                   reason: str = "") -> None:
    await execute(
        """INSERT INTO mutes(owner_id,chat_id,user_id,until,reason,created_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(owner_id,chat_id,user_id) DO UPDATE SET
                until=excluded.until, reason=excluded.reason,
                created_at=excluded.created_at""",
        (owner_id, chat_id, user_id, until, reason, now()))


async def get_mute(owner_id: int, chat_id: int, user_id: int):
    return await fetchone(
        "SELECT * FROM mutes WHERE owner_id=? AND chat_id=? AND user_id=?",
        (owner_id, chat_id, user_id))


async def remove_mute(owner_id: int, chat_id: int, user_id: int) -> None:
    await execute("DELETE FROM mutes WHERE owner_id=? AND chat_id=? AND user_id=?",
                  (owner_id, chat_id, user_id))


async def all_mutes(owner_id: int | None = None):
    if owner_id is None:
        return await fetchall("SELECT * FROM mutes")
    return await fetchall("SELECT * FROM mutes WHERE owner_id=?", (owner_id,))


# ---------------------------------------------------------- срочные вызовы --

async def last_urgent_call(owner_id: int, user_id: int) -> int:
    """Когда этот человек дёргал владельца в прошлый раз. 0 — никогда."""
    return await scalar(
        "SELECT MAX(at) FROM urgent_calls WHERE owner_id=? AND user_id=?",
        (owner_id, user_id))


async def add_urgent_call(owner_id: int, user_id: int, user_name: str | None,
                          chat_id: int | None, reason: str) -> None:
    await execute(
        "INSERT INTO urgent_calls(owner_id,user_id,user_name,chat_id,reason,at) "
        "VALUES (?,?,?,?,?,?)",
        (owner_id, user_id, user_name, chat_id, reason, now()))


async def urgent_calls(owner_id: int, limit: int = 20):
    return await fetchall(
        "SELECT * FROM urgent_calls WHERE owner_id=? ORDER BY at DESC LIMIT ?",
        (owner_id, limit))


# ------------------------------------------------------------ белый список --

async def allow(owner_id: int, user_id: int, name: str) -> None:
    await execute("INSERT INTO allowlist(owner_id,user_id,name,added_at) "
                  "VALUES (?,?,?,?) "
                  "ON CONFLICT(owner_id,user_id) DO UPDATE SET name=excluded.name",
                  (owner_id, user_id, name, now()))


async def disallow(owner_id: int, user_id: int) -> bool:
    row = await fetchone("SELECT user_id FROM allowlist WHERE owner_id=? AND user_id=?",
                         (owner_id, user_id))
    await execute("DELETE FROM allowlist WHERE owner_id=? AND user_id=?",
                  (owner_id, user_id))
    return row is not None


async def allowed_users(owner_id: int | None = None):
    if owner_id is None:
        return await fetchall("SELECT * FROM allowlist ORDER BY added_at")
    return await fetchall("SELECT * FROM allowlist WHERE owner_id=? ORDER BY added_at",
                          (owner_id,))


# ---------------------------------------------------------------- поиск -----

def _escape_like(query: str) -> str:
    return query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def search_deleted(owner_id: int, query: str, limit: int = 20):
    """Поиск по журналу удалённых и перехваченных одного владельца."""
    pattern = f"%{_escape_like(query)}%"
    return await fetchall(
        """SELECT chat_id, user_id, user_name, text, media_type, file_id,
                  date, deleted_at AS at, 'deleted' AS kind
             FROM deleted   WHERE owner_id=? AND text LIKE ? ESCAPE '\\'
           UNION ALL
           SELECT chat_id, user_id, user_name, text, media_type, file_id,
                  date, at, 'intercepted' AS kind
             FROM intercepted WHERE owner_id=? AND text LIKE ? ESCAPE '\\'
           ORDER BY at DESC LIMIT ?""",
        (owner_id, pattern, owner_id, pattern, limit))


# ---------------------------------------------------------------- business ---

async def save_business(connection_id: str, user_id: int, user_chat_id: int | None,
                        is_enabled: bool, rights: str) -> None:
    await execute(
        """INSERT INTO business(connection_id,user_id,user_chat_id,is_enabled,
                                rights,connected_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(connection_id) DO UPDATE SET
                user_id=excluded.user_id,
                user_chat_id=excluded.user_chat_id,
                is_enabled=excluded.is_enabled,
                rights=excluded.rights""",
        (connection_id, user_id, user_chat_id, int(is_enabled), rights, now()))


async def all_business():
    return await fetchall("SELECT * FROM business")


async def drop_business(connection_id: str) -> None:
    await execute("DELETE FROM business WHERE connection_id=?", (connection_id,))


# ---------------------------------------------------------------- settings ---

DEFAULT_SETTINGS = {
    "antidelete": None,
    "log_edits": None,
    "save_media": None,
    "ignored": 0,
}


async def get_settings(owner_id: int, chat_id: int) -> dict:
    row = await fetchone("SELECT * FROM settings WHERE owner_id=? AND chat_id=?",
                         (owner_id, chat_id))
    if row is None:
        return dict(DEFAULT_SETTINGS)
    return {k: row[k] for k in DEFAULT_SETTINGS}


async def set_setting(owner_id: int, chat_id: int, key: str, value: int,
                      title: str | None = None) -> None:
    if key not in DEFAULT_SETTINGS:
        raise KeyError(key)
    await execute(
        f"INSERT INTO settings(owner_id,chat_id,{key},title) VALUES (?,?,?,?) "
        f"ON CONFLICT(owner_id,chat_id) DO UPDATE SET {key}=excluded.{key}, "
        f"title=COALESCE(excluded.title, settings.title)",
        (owner_id, chat_id, value, title))


async def activity(owner_id: int) -> dict:
    """Когда бот в последний раз что-то видел и о чём-то сообщал."""
    return {
        "cached": await scalar(
            "SELECT COUNT(*) FROM messages WHERE owner_id=?", (owner_id,)),
        "last_seen": await scalar(
            "SELECT MAX(date) FROM messages WHERE owner_id=?", (owner_id,), None),
        "last_report": await scalar(
            "SELECT MAX(deleted_at) FROM deleted WHERE owner_id=?", (owner_id,), None),
        "last_intercept": await scalar(
            "SELECT MAX(at) FROM intercepted WHERE owner_id=?", (owner_id,), None),
    }


async def name_of(owner_id: int, user_id: int) -> str | None:
    """Как звали человека — берём из последнего, что от него сохранилось."""
    row = await fetchone(
        """SELECT user_name FROM intercepted
            WHERE owner_id=? AND user_id=? AND user_name IS NOT NULL
            ORDER BY at DESC LIMIT 1""", (owner_id, user_id))
    if row is None:
        row = await fetchone(
            """SELECT user_name FROM deleted
                WHERE owner_id=? AND user_id=? AND user_name IS NOT NULL
                ORDER BY deleted_at DESC LIMIT 1""", (owner_id, user_id))
    if row is None:
        row = await fetchone(
            """SELECT user_name FROM messages
                WHERE owner_id=? AND user_id=? AND user_name IS NOT NULL
                ORDER BY date DESC LIMIT 1""", (owner_id, user_id))
    return row["user_name"] if row else None


async def chat_title(owner_id: int, chat_id: int) -> str | None:
    row = await fetchone(
        "SELECT title FROM settings WHERE owner_id=? AND chat_id=? AND title IS NOT NULL",
        (owner_id, chat_id))
    if row:
        return row["title"]
    return await name_of(owner_id, chat_id)


async def tuned_chats(owner_id: int):
    """Чаты, для которых владелец что-то менял руками."""
    return await fetchall(
        "SELECT * FROM settings WHERE owner_id=? AND "
        "(ignored=1 OR antidelete IS NOT NULL OR log_edits IS NOT NULL "
        " OR save_media IS NOT NULL) ORDER BY chat_id",
        (owner_id,))


async def reset_chat(owner_id: int, chat_id: int) -> None:
    """Возвращает чату поведение по умолчанию."""
    await execute("DELETE FROM settings WHERE owner_id=? AND chat_id=?",
                  (owner_id, chat_id))


# ------------------------------------------------------------------- notes ---

async def save_note(owner_id: int, chat_id: int, name: str, content: str) -> None:
    await execute(
        "INSERT INTO notes(owner_id,chat_id,name,content) VALUES (?,?,?,?) "
        "ON CONFLICT(owner_id,chat_id,name) DO UPDATE SET content=excluded.content",
        (owner_id, chat_id, name.lower(), content))


async def get_note(owner_id: int, chat_id: int, name: str):
    return await fetchone(
        "SELECT content FROM notes WHERE owner_id=? AND chat_id=? AND name=?",
        (owner_id, chat_id, name.lower()))


async def list_notes(owner_id: int, chat_id: int):
    return await fetchall(
        "SELECT name FROM notes WHERE owner_id=? AND chat_id=? ORDER BY name",
        (owner_id, chat_id))


async def drop_note(owner_id: int, chat_id: int, name: str) -> None:
    await execute("DELETE FROM notes WHERE owner_id=? AND chat_id=? AND name=?",
                  (owner_id, chat_id, name.lower()))


# ---------------------------------------------------------------------- kv ---

async def kv_get(key: str, default=None):
    row = await fetchone("SELECT v FROM kv WHERE k=?", (key,))
    return row["v"] if row else default


async def kv_set(key: str, value: str) -> None:
    await execute("INSERT INTO kv(k,v) VALUES (?,?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, str(value)))


# ---------------------------------------------------------------- служебка ---

async def cleanup() -> tuple[int, int]:
    """Чистка кэша по TTL. Возвращает (удалено кэша, удалено из журнала)."""
    cutoff = now() - config.CACHE_TTL_HOURS * 3600
    cur = await conn().execute("DELETE FROM messages WHERE date < ?", (cutoff,))
    a = cur.rowcount or 0
    cutoff2 = now() - config.DELETED_TTL_DAYS * 86400
    cur = await conn().execute("DELETE FROM deleted WHERE deleted_at < ?", (cutoff2,))
    b = cur.rowcount or 0
    await conn().execute("DELETE FROM edits WHERE edited_at < ?", (cutoff2,))
    await conn().execute("DELETE FROM intercepted WHERE at < ?", (cutoff2,))
    await conn().execute("DELETE FROM urgent_calls WHERE at < ?", (cutoff2,))
    # Историю имён по сроку не чистим: она маленькая, а её ценность как раз в
    # том, что она длинная. Уходит только вместе с самим человеком.
    await conn().commit()
    return a, b


async def stats(owner_id: int | None = None) -> dict:
    def scope(table: str) -> tuple[str, tuple]:
        if owner_id is None:
            return f"SELECT COUNT(*) FROM {table}", ()
        return f"SELECT COUNT(*) FROM {table} WHERE owner_id=?", (owner_id,)

    counts = {}
    for key, table in (("cached", "messages"), ("deleted", "deleted"),
                       ("edits", "edits"), ("mutes", "mutes"),
                       ("notes", "notes"), ("intercepted", "intercepted"),
                       ("allowed", "allowlist")):
        sql, params = scope(table)
        counts[key] = await scalar(sql, params)
    counts["business"] = await scalar(
        "SELECT COUNT(*) FROM business WHERE is_enabled=1"
        + (" AND user_id=?" if owner_id is not None else ""),
        (owner_id,) if owner_id is not None else ())
    counts["users"] = await scalar("SELECT COUNT(*) FROM users WHERE status='approved'")
    counts["size"] = config.DB_PATH.stat().st_size if config.DB_PATH.exists() else 0
    return counts
