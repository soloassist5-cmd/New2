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
    if state.log_entity is None and state.api is None:
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
        # Режим Business: юзербота нет, копию присылает бот. Чтобы поднять её
        # обратно после передеплоя, достаточно переслать файл боту.
        await state.api.send_file(
            state.owner_chat_id, tmp.read_bytes(), "guard.sqlite3",
            caption=caption + "\n\n_Перешлите этот файл боту, чтобы восстановить "
                    "базу после передеплоя._")
        return True
    except Exception as e:                                   # noqa: BLE001
        log.error("не удалось отправить бэкап: %r", e)
        return False
    finally:
        tmp.unlink(missing_ok=True)


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
