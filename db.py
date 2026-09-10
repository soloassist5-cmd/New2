"""Слой доступа к SQLite (aiosqlite). Один процесс — одно соединение."""
from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

import aiosqlite

import config

_conn: aiosqlite.Connection | None = None

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS messages (
    chat_id    INTEGER NOT NULL,
    msg_id     INTEGER NOT NULL,
    user_id    INTEGER,
    is_private INTEGER NOT NULL DEFAULT 0,
    text       TEXT,
    media_type TEXT,
    media_ref  INTEGER,
    reply_to   INTEGER,
    date       INTEGER NOT NULL,
    PRIMARY KEY (chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_msg_id   ON messages(msg_id);
CREATE INDEX IF NOT EXISTS idx_msg_date ON messages(date);

CREATE TABLE IF NOT EXISTS deleted (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER,
    msg_id     INTEGER,
    user_id    INTEGER,
    text       TEXT,
    media_type TEXT,
    media_ref  INTEGER,
    date       INTEGER,
    deleted_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_del_chat ON deleted(chat_id, deleted_at);

CREATE TABLE IF NOT EXISTS edits (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER,
    msg_id     INTEGER,
    user_id    INTEGER,
    old_text   TEXT,
    new_text   TEXT,
    edited_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS mutes (
    chat_id    INTEGER NOT NULL,   -- 0 = глобальный мут
    user_id    INTEGER NOT NULL,
    until      INTEGER NOT NULL DEFAULT 0,  -- 0 = бессрочно
    reason     TEXT,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS settings (
    chat_id    INTEGER PRIMARY KEY,
    antidelete INTEGER,
    log_edits  INTEGER,
    save_media INTEGER,
    ignored    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS notes (
    chat_id INTEGER NOT NULL,
    name    TEXT NOT NULL,
    content TEXT,
    PRIMARY KEY (chat_id, name)
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);

-- Подключения Telegram Business: бот, добавленный в Настройки -> Telegram
-- Business -> Чат-боты, получает права на личные чаты владельца.
CREATE TABLE IF NOT EXISTS business (
    connection_id TEXT PRIMARY KEY,
    user_id       INTEGER NOT NULL,
    user_chat_id  INTEGER,
    is_enabled    INTEGER NOT NULL DEFAULT 1,
    rights        TEXT,
    connected_at  INTEGER NOT NULL
);
"""

# Добавляются к уже существующим базам без потери данных.
MIGRATIONS = {
    "messages": {"file_id": "TEXT", "business_id": "TEXT", "user_name": "TEXT"},
    "deleted": {"file_id": "TEXT", "user_name": "TEXT"},
}


def now() -> int:
    return int(time.time())


async def init() -> None:
    global _conn
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = await aiosqlite.connect(config.DB_PATH)
    connection.row_factory = aiosqlite.Row
    try:
        await connection.executescript(SCHEMA)
        _conn = connection
        await _migrate()
        await _conn.commit()
    except Exception:
        # Иначе останется висеть рабочий поток aiosqlite и битое соединение.
        _conn = None
        await connection.close()
        raise


async def _migrate() -> None:
    """Досоздаёт колонки, появившиеся в новых версиях."""
    for table, columns in MIGRATIONS.items():
        async with _conn.execute(f"PRAGMA table_info({table})") as cur:
            existing = {row[1] for row in await cur.fetchall()}
        for name, sql_type in columns.items():
            if name not in existing:
                await _conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


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


# ---------------------------------------------------------------- messages ---

async def cache_message(
    chat_id: int,
    msg_id: int,
    user_id: int | None,
    is_private: bool,
    text: str | None,
    media_type: str | None,
    media_ref: int | None,
    reply_to: int | None,
    date: int,
    file_id: str | None = None,
    business_id: str | None = None,
    user_name: str | None = None,
) -> None:
    await execute(
        """INSERT INTO messages(chat_id,msg_id,user_id,is_private,text,media_type,
                                media_ref,reply_to,date,file_id,business_id,user_name)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(chat_id,msg_id) DO UPDATE SET
                text=excluded.text,
                media_type=excluded.media_type,
                media_ref=COALESCE(excluded.media_ref, messages.media_ref),
                file_id=COALESCE(excluded.file_id, messages.file_id),
                business_id=COALESCE(excluded.business_id, messages.business_id),
                user_name=COALESCE(excluded.user_name, messages.user_name)""",
        (chat_id, msg_id, user_id, int(is_private), text, media_type,
         media_ref, reply_to, date, file_id, business_id, user_name),
    )


async def set_media_ref(chat_id: int, msg_id: int, ref: int) -> None:
    await execute("UPDATE messages SET media_ref=? WHERE chat_id=? AND msg_id=?",
                  (ref, chat_id, msg_id))


async def get_message(chat_id: int, msg_id: int):
    return await fetchone("SELECT * FROM messages WHERE chat_id=? AND msg_id=?",
                          (chat_id, msg_id))


async def find_private_message(msg_id: int):
    """UpdateDeleteMessages в личках не содержит chat_id — ищем по одному msg_id."""
    return await fetchone(
        "SELECT * FROM messages WHERE msg_id=? AND is_private=1 "
        "ORDER BY date DESC LIMIT 1",
        (msg_id,),
    )


async def drop_message(chat_id: int, msg_id: int) -> None:
    await execute("DELETE FROM messages WHERE chat_id=? AND msg_id=?", (chat_id, msg_id))


# ----------------------------------------------------------------- deleted ---

async def add_deleted(row: dict) -> int:
    cur = await conn().execute(
        """INSERT INTO deleted(chat_id,msg_id,user_id,text,media_type,media_ref,
                               date,deleted_at,file_id,user_name)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (row.get("chat_id"), row.get("msg_id"), row.get("user_id"), row.get("text"),
         row.get("media_type"), row.get("media_ref"), row.get("date"), now(),
         row.get("file_id"), row.get("user_name")),
    )
    await conn().commit()
    return cur.lastrowid


async def last_deleted(chat_id: int | None, limit: int = 10):
    if chat_id is None:
        return await fetchall(
            "SELECT * FROM deleted ORDER BY deleted_at DESC LIMIT ?", (limit,))
    return await fetchall(
        "SELECT * FROM deleted WHERE chat_id=? ORDER BY deleted_at DESC LIMIT ?",
        (chat_id, limit))


async def add_edit(chat_id, msg_id, user_id, old_text, new_text) -> None:
    await execute(
        "INSERT INTO edits(chat_id,msg_id,user_id,old_text,new_text,edited_at) "
        "VALUES (?,?,?,?,?,?)",
        (chat_id, msg_id, user_id, old_text, new_text, now()),
    )


# ------------------------------------------------------------------- mutes ---

async def add_mute(chat_id: int, user_id: int, until: int, reason: str = "") -> None:
    await execute(
        """INSERT INTO mutes(chat_id,user_id,until,reason,created_at) VALUES (?,?,?,?,?)
           ON CONFLICT(chat_id,user_id) DO UPDATE SET until=excluded.until,
                                                      reason=excluded.reason,
                                                      created_at=excluded.created_at""",
        (chat_id, user_id, until, reason, now()),
    )


async def remove_mute(chat_id: int, user_id: int) -> None:
    await execute("DELETE FROM mutes WHERE chat_id=? AND user_id=?", (chat_id, user_id))


async def all_mutes():
    return await fetchall("SELECT * FROM mutes")


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
        (connection_id, user_id, user_chat_id, int(is_enabled), rights, now()),
    )


async def all_business():
    return await fetchall("SELECT * FROM business")


async def drop_business(connection_id: str) -> None:
    await execute("DELETE FROM business WHERE connection_id=?", (connection_id,))


# ---------------------------------------------------------------- settings ---

DEFAULT_SETTINGS = {
    "antidelete": None,   # None -> вычисляется по типу чата
    "log_edits": None,
    "save_media": None,
    "ignored": 0,
}


async def get_settings(chat_id: int) -> dict:
    row = await fetchone("SELECT * FROM settings WHERE chat_id=?", (chat_id,))
    if row is None:
        return dict(DEFAULT_SETTINGS)
    return {k: row[k] for k in DEFAULT_SETTINGS}


async def set_setting(chat_id: int, key: str, value: int) -> None:
    if key not in DEFAULT_SETTINGS:
        raise KeyError(key)
    await execute(
        f"INSERT INTO settings(chat_id,{key}) VALUES (?,?) "
        f"ON CONFLICT(chat_id) DO UPDATE SET {key}=excluded.{key}",
        (chat_id, value),
    )


# ------------------------------------------------------------------- notes ---

async def save_note(chat_id: int, name: str, content: str) -> None:
    await execute(
        "INSERT INTO notes(chat_id,name,content) VALUES (?,?,?) "
        "ON CONFLICT(chat_id,name) DO UPDATE SET content=excluded.content",
        (chat_id, name.lower(), content),
    )


async def get_note(chat_id: int, name: str):
    return await fetchone("SELECT content FROM notes WHERE chat_id=? AND name=?",
                          (chat_id, name.lower()))


async def list_notes(chat_id: int):
    return await fetchall("SELECT name FROM notes WHERE chat_id=? ORDER BY name", (chat_id,))


async def drop_note(chat_id: int, name: str) -> None:
    await execute("DELETE FROM notes WHERE chat_id=? AND name=?", (chat_id, name.lower()))


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
    await conn().commit()
    return a, b


async def stats() -> dict:
    return {
        "cached": await scalar("SELECT COUNT(*) FROM messages"),
        "deleted": await scalar("SELECT COUNT(*) FROM deleted"),
        "edits": await scalar("SELECT COUNT(*) FROM edits"),
        "mutes": await scalar("SELECT COUNT(*) FROM mutes"),
        "notes": await scalar("SELECT COUNT(*) FROM notes"),
        "business": await scalar("SELECT COUNT(*) FROM business WHERE is_enabled=1"),
        "size": config.DB_PATH.stat().st_size if config.DB_PATH.exists() else 0,
    }
