"""Точка входа.

Два независимых режима, любой можно включить отдельно:

* **Telegram Business** (нужен только BOT_TOKEN) — бот, подключённый в
  Настройки → Telegram Business → Чат-боты, получает события личных чатов,
  включая удаления. Ни строки сессии, ни API-ключей не требуется.
* **Юзербот** (SESSION + API_ID/API_HASH) — MTProto-клиент от вашего аккаунта:
  работает и в группах, но это серая зона правил Telegram.

Если заданы оба, они дополняют друг друга: бот отвечает за личные чаты и
доставку отчётов, юзербот — за группы.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from logging.handlers import RotatingFileHandler

import config
import db
import web
from bot import business, commands, poller
from bot.api import BotAPI
from core import backup, dispatcher, fmt, reporter, state

LOG_FILE = config.ROOT / "data" / "guard.log"


def setup_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(sys.stdout),
                RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=2,
                                    encoding="utf-8")]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s",
        handlers=handlers,
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


log = logging.getLogger("main")


async def start_userbot():
    """Поднимает MTProto-клиент, если задана строка сессии."""
    if not config.userbot_enabled():
        if config.SESSION:
            log.error("SESSION задан, но нет API_ID/API_HASH — юзербот выключен")
        return None

    from telethon import TelegramClient
    from telethon.sessions import StringSession

    import modules

    client = TelegramClient(
        StringSession(config.SESSION), config.API_ID, config.API_HASH,
        connection_retries=None, retry_delay=5, auto_reconnect=True,
        device_model="Guard", system_version="1.0", app_version="1.0",
    )
    await client.connect()
    if not await client.is_user_authorized():
        log.error("SESSION недействителен или отозван. Сгенерируйте новый: "
                  "python scripts/gen_session.py")
        await client.disconnect()
        return None

    state.client = client
    state.me = await client.get_me()
    state.owner_id = state.owner_id or state.me.id
    log.info("юзербот: вошли как %s (id %s)", fmt.name_of(state.me), state.me.id)

    try:
        state.log_entity = await client.get_entity(config.LOG_CHAT)
    except Exception as e:                                   # noqa: BLE001
        log.warning("LOG_CHAT=%r недоступен (%r) — использую «Избранное»",
                    config.LOG_CHAT, e)
        try:
            state.log_entity = await client.get_entity("me")
        except Exception:
            state.log_entity = None

    dispatcher.setup(client)
    modules.setup_all(client)
    log.info("команд юзербота: %s", len(dispatcher.COMMANDS))
    return client


async def start_bot():
    """Поднимает Bot API-клиент и восстанавливает бизнес-подключения."""
    if not config.BOT_TOKEN:
        log.info("BOT_TOKEN не задан — режим Business выключен")
        return None

    api = BotAPI(config.BOT_TOKEN)
    await api.start()
    state.api = api
    state.bot_user = await api.get_me()
    log.info("бот @%s готов", state.bot_user.get("username"))

    await business.load_connections()
    if config.OWNER_ID:
        state.owner_id = config.OWNER_ID
        state.owner_chat_id = state.owner_chat_id or config.OWNER_ID
    await commands.publish_menu(api)

    if not state.business:
        log.info("бизнес-подключений нет: откройте чат с ботом и нажмите Start")
    return api


async def start_transport(api, stop: asyncio.Event) -> list[asyncio.Task]:
    """Вебхук, если задан публичный адрес, иначе long polling."""
    if config.use_webhook():
        url = config.WEBHOOK_URL + config.webhook_path()
        try:
            await api.set_webhook(url, config.webhook_secret(),
                                  poller.ALLOWED_UPDATES)
            log.info("вебхук установлен: %s", url)
            return []
        except Exception as e:                               # noqa: BLE001
            log.error("не удалось установить вебхук (%r) — падаю на long polling", e)

    try:
        # Оставшийся вебхук молча блокирует getUpdates.
        await api.delete_webhook()
    except Exception as e:                                   # noqa: BLE001
        log.debug("deleteWebhook: %r", e)
    log.info("режим long polling")
    return [asyncio.create_task(poller.run(api, stop))]


async def announce_restart() -> None:
    """Дописывает «перезапущен» к сообщению, из которого вызвали .restart."""
    chat = await db.kv_get("restart_chat")
    msg_id = await db.kv_get("restart_msg")
    if not chat or not msg_id or state.client is None:
        return
    try:
        await state.client.edit_message(int(chat), int(msg_id), "♻️ **Перезапущен.**")
    except Exception:
        pass
    finally:
        await db.kv_set("restart_chat", "")
        await db.kv_set("restart_msg", "")


def describe_modes() -> str:
    modes = []
    if state.api is not None:
        connected = sum(1 for i in state.business.values() if i.get("is_enabled"))
        modes.append(f"бот @{(state.bot_user or {}).get('username', '?')}"
                     + (f", подключений Business: {connected}" if connected
                        else ", ожидает подключения"))
    if state.client is not None:
        modes.append(f"юзербот {fmt.name_of(state.me)}")
    return " · ".join(modes) or "нет активных режимов"


async def run() -> None:
    problems = config.validate()
    if problems:
        for item in problems:
            log.error("конфигурация: %s", item)
        log.error("Заполните .env — пример со всеми полями лежит в .env.example")
        sys.exit(1)

    state.start_time = time.time()
    client = await start_userbot()

    if config.RESTORE_ON_START and not config.DB_PATH.exists() and client is not None:
        log.info("базы нет — пробую восстановить из лог-чата")
        await backup.restore()

    await db.init()
    await state.load_mutes()
    log.info("мутов загружено: %s", len(state.mutes))

    api = await start_bot()
    if client is None and api is None:
        log.error("не удалось запустить ни один режим")
        sys.exit(1)

    runner = await web.start()
    tasks = [asyncio.create_task(backup.loop())]
    stop = asyncio.Event()
    if api is not None:
        tasks.extend(await start_transport(api, stop))

    await announce_restart()
    log.info("режимы: %s", describe_modes())
    await reporter.send_report(
        f"🛡 **Guard запущен**\n"
        f"{describe_modes()}\n"
        f"📡 транспорт: {'вебхук' if config.use_webhook() else 'long polling'}\n"
        f"⌨️ префикс `{config.PREFIX}`\n"
        f"🔇 мутов восстановлено: {len(state.mutes)}"
    )

    try:
        if client is not None:
            await client.run_until_disconnected()
        else:
            await stop.wait()
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        if runner is not None:
            await runner.cleanup()
        if api is not None:
            await api.close()
        await db.close()


def main() -> None:
    setup_logging()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("остановлен вручную")


if __name__ == "__main__":
    main()
