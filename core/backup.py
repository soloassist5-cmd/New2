"""Бэкап БД в лог-чат и восстановление оттуда.

Нужен на бесплатных хостах с эфемерным диском: контейнер пересоздаётся —
база подтягивается из Telegram при старте.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

import config
import db
from core import fmt, state

log = logging.getLogger("backup")

KEEP = 5          # сколько последних бэкапов держать в лог-чате


async def make_backup() -> Path | None:
    """Согласованная копия БД во временном файле."""
    tmp = Path(tempfile.gettempdir()) / f"guard_backup_{db.now()}.sqlite3"
    try:
        await db.conn().execute("VACUUM INTO ?", (str(tmp),))
        return tmp
    except Exception as e:                                   # noqa: BLE001
        log.warning("VACUUM INTO не сработал (%r), копирую файл как есть", e)
        try:
            tmp.write_bytes(config.DB_PATH.read_bytes())
            return tmp
        except Exception as e2:                              # noqa: BLE001
            log.error("бэкап не создан: %r", e2)
            return None


async def upload() -> bool:
    if state.log_entity is None and (state.api is None or not config.OWNER_ID):
        return False
    tmp = await make_backup()
    if tmp is None:
        return False
    try:
        stats = await db.stats()
        caption = (f"{config.BACKUP_TAG}\n🗄 Бэкап базы · {fmt.ts(db.now())}\n"
                   f"кэш {stats['cached']} · удалённых {stats['deleted']} · "
                   f"размер {fmt.size(stats['size'])}")
        if state.log_entity is not None:
            await state.client.send_file(state.log_entity, tmp, caption=caption,
                                         force_document=True)
            await _prune()
            return True
        # Режим Business: юзербота нет, копию присылает и закрепляет бот.
        # Закреплённое сообщение — единственное, что бот может прочитать у себя
        # в личке при старте (getChat), поэтому именно оттуда база и поднимается
        # после передеплоя на хостинге с временным диском.
        sent = await state.api.send_file(
            state.chat_of(config.OWNER_ID), tmp.read_bytes(), BACKUP_NAME,
            caption=caption + "\n\n_Это сообщение закреплено: из него база "
                    "восстановится сама после передеплоя._")
        await _repin(sent)
        return True
    except Exception as e:                                   # noqa: BLE001
        log.error("не удалось отправить бэкап: %r", e)
        return False
    finally:
        tmp.unlink(missing_ok=True)


BACKUP_NAME = "guard.sqlite3"
KV_PINNED = "pinned_backup"


async def _repin(sent: dict | None) -> None:
    """Закрепляет свежую копию и снимает закрепление с прошлой."""
    if not sent or not sent.get("message_id"):
        return
    previous = await db.kv_get(KV_PINNED, "")
    admin_chat = state.chat_of(config.OWNER_ID)
    try:
        await state.api.pin_message(admin_chat, sent["message_id"])
        await db.kv_set(KV_PINNED, sent["message_id"])
    except Exception as e:                                   # noqa: BLE001
        log.warning("не удалось закрепить бэкап: %r", e)
        return
    if previous:
        try:
            await state.api.unpin_message(admin_chat, int(previous))
        except Exception as e:                               # noqa: BLE001
            log.debug("старое закрепление не снялось: %r", e)


async def restore_from_pinned(api, owner_chat_id: int) -> bool:
    """Поднимает базу из закреплённого в личке бэкапа.

    История чата боту недоступна, но закреплённое сообщение отдаётся в getChat —
    на этом и держится восстановление после передеплоя.
    """
    if not owner_chat_id:
        return False
    try:
        chat = await api.get_chat(owner_chat_id)
    except Exception as e:                                   # noqa: BLE001
        log.warning("не удалось открыть личку владельца: %r", e)
        return False

    document = (chat.get("pinned_message") or {}).get("document") or {}
    name = document.get("file_name") or ""
    if not name.endswith((".sqlite3", ".db")):
        log.info("закреплённого бэкапа в личке нет")
        return False

    incoming = config.DB_PATH.with_suffix(".incoming")
    try:
        info = await api.get_file(document["file_id"])
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        incoming.write_bytes(await api.download(info["file_path"]))
    except Exception as e:                                   # noqa: BLE001
        log.warning("не удалось скачать закреплённый бэкап: %r", e)
        incoming.unlink(missing_ok=True)
        return False

    if not await db.is_valid_database(incoming):
        log.warning("закреплённый файл не похож на базу Guard")
        incoming.unlink(missing_ok=True)
        return False

    incoming.replace(config.DB_PATH)
    log.info("база восстановлена из закреплённого бэкапа")
    return True


async def _prune() -> None:
    try:
        old = []
        async for msg in state.client.iter_messages(state.log_entity,
                                                    search=config.BACKUP_TAG, limit=50):
            if msg.document:
                old.append(msg.id)
        if len(old) > KEEP:
            await state.client.delete_messages(state.log_entity, old[KEEP:])
    except Exception as e:                                   # noqa: BLE001
        log.debug("чистка старых бэкапов: %r", e)


async def restore() -> bool:
    """Скачивает свежий бэкап в DB_PATH. Возвращает True, если получилось."""
    if state.log_entity is None:
        return False
    try:
        async for msg in state.client.iter_messages(state.log_entity,
                                                    search=config.BACKUP_TAG, limit=20):
            if not msg.document:
                continue
            config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            await state.client.download_media(msg, file=str(config.DB_PATH))
            log.info("база восстановлена из бэкапа от %s", fmt.ts(int(msg.date.timestamp())))
            return True
    except Exception as e:                                   # noqa: BLE001
        log.warning("восстановление не удалось: %r", e)
    return False


async def loop() -> None:
    """Фоновая задача: периодический бэкап + чистка кэша по TTL."""
    if config.BACKUP_EVERY_MIN <= 0:
        return
    while True:
        await asyncio.sleep(config.BACKUP_EVERY_MIN * 60)
        try:
            removed, purged = await db.cleanup()
            log.info("TTL-чистка: кэш -%s, журнал -%s", removed, purged)
            await upload()
        except asyncio.CancelledError:
            raise
        except Exception as e:                               # noqa: BLE001
            log.warning("фоновая задача: %r", e)
