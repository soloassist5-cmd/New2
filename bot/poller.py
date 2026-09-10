"""Long polling Bot API и маршрутизация апдейтов."""
from __future__ import annotations

import asyncio
import logging

from bot import business, commands
from bot.api import BotAPIError

log = logging.getLogger("poller")

# Business-апдейты приходят, только если явно запросить их здесь.
ALLOWED_UPDATES = [
    "message",
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
]

POLL_TIMEOUT = 25          # меньше таймаута HTTP-сессии
BACKOFF_START = 2
BACKOFF_MAX = 60


async def dispatch(api, update: dict) -> None:
    if "business_connection" in update:
        await business.on_business_connection(update["business_connection"])
    elif "business_message" in update:
        await business.on_business_message(api, update["business_message"])
    elif "edited_business_message" in update:
        await business.on_edited_business_message(update["edited_business_message"])
    elif "deleted_business_messages" in update:
        await business.on_deleted_business_messages(update["deleted_business_messages"])
    elif "message" in update:
        await commands.handle(api, update["message"])


async def run(api, stop: asyncio.Event | None = None) -> None:
    offset: int | None = None
    backoff = BACKOFF_START

    while stop is None or not stop.is_set():
        try:
            updates = await api.get_updates(offset, POLL_TIMEOUT, ALLOWED_UPDATES)
            backoff = BACKOFF_START
        except BotAPIError as e:
            if e.code == 409:
                log.error("другой процесс уже читает апдейты этого бота "
                          "(запущено два экземпляра?) — жду %s с", backoff)
            elif e.code == 401:
                log.error("BOT_TOKEN недействителен — опрос остановлен")
                return
            else:
                log.warning("getUpdates: %s", e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
            continue
        except asyncio.CancelledError:
            raise
        except Exception as e:                               # noqa: BLE001
            log.warning("сеть недоступна (%r), повтор через %s с", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            try:
                await dispatch(api, update)
            except asyncio.CancelledError:
                raise
            except Exception:                                # noqa: BLE001
                log.exception("апдейт %s не обработан", update.get("update_id"))
