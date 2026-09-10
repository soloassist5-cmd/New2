"""Точка входа: Telegram Guard (юзербот на Telethon)."""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from logging.handlers import RotatingFileHandler

from telethon import TelegramClient
from telethon.sessions import StringSession

import config
import db
import health
import modules
from core import backup, dispatcher, fmt, reporter, state
from modules import botui

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


async def resolve_log_chat(client) -> None:
    try:
        state.log_entity = await client.get_entity(config.LOG_CHAT)
        log.info("лог-чат: %s", fmt.name_of(state.log_entity))
    except Exception as e:                                   # noqa: BLE001
        log.error("не удалось открыть LOG_CHAT=%r (%r) — падаю на «Избранное»",
                  config.LOG_CHAT, e)
        try:
            state.log_entity = await client.get_entity("me")
        except Exception:
            state.log_entity = None


async def start_bot() -> None:
    """Поднимает бота-почтальона, если задан BOT_TOKEN."""
    if not config.report_via_bot():
        log.info("BOT_TOKEN не задан — отчёты пойдут в LOG_CHAT от вашего аккаунта")
        return
    bot = TelegramClient(StringSession(), config.API_ID, config.API_HASH)
    await bot.start(bot_token=config.BOT_TOKEN)
    state.bot = bot
    state.owner_id = config.OWNER_ID or (state.me.id if state.me else 0)

    botui.setup_bot(bot)
    await botui.publish_menu(bot)

    known = await reporter.resolve_owner()
    me_bot = await bot.get_me()
    log.info("бот @%s готов, владелец %s%s", me_bot.username, state.owner_id,
             "" if known else " (ждёт вашего /start)")


async def announce_restart() -> None:
    """Дописывает «перезапущен» к сообщению, из которого вызвали .restart."""
    chat = await db.kv_get("restart_chat")
    msg_id = await db.kv_get("restart_msg")
    if not chat or not msg_id:
        return
    try:
        await state.client.edit_message(int(chat), int(msg_id), "♻️ **Перезапущен.**")
    except Exception:
        pass
    finally:
        await db.kv_set("restart_chat", "")
        await db.kv_set("restart_msg", "")


async def run() -> None:
    problems = config.validate()
    if problems:
        for item in problems:
            log.error("конфигурация: %s", item)
        log.error("Заполните .env (пример — .env.example) и запустите снова.")
        sys.exit(1)

    state.start_time = time.time()
    client = TelegramClient(
        StringSession(config.SESSION), config.API_ID, config.API_HASH,
        connection_retries=None,      # бесконечные переподключения
        retry_delay=5,
        auto_reconnect=True,
        device_model="Guard", system_version="1.0", app_version="1.0",
    )
    state.client = client

    await client.connect()
    if not await client.is_user_authorized():
        log.error("SESSION недействителен или отозван. Сгенерируйте новый: "
                  "python scripts/gen_session.py")
        await client.disconnect()
        sys.exit(1)
    state.me = await client.get_me()
    log.info("вошли как %s (id %s)", fmt.name_of(state.me), state.me.id)

    await resolve_log_chat(client)

    if config.RESTORE_ON_START and not config.DB_PATH.exists():
        log.info("базы нет — пробую восстановить из лог-чата")
        await backup.restore()

    await db.init()
    await state.load_mutes()
    log.info("мутов загружено: %s", len(state.mutes))

    dispatcher.setup(client)
    modules.setup_all(client)
    log.info("команд зарегистрировано: %s", len(dispatcher.COMMANDS))

    await start_bot()

    runner = await health.start()
    task = asyncio.create_task(backup.loop())

    await announce_restart()
    where = "через бота" if state.bot is not None else "в лог-чат"
    await reporter.send_report(
        f"🛡 **Guard запущен**\n"
        f"👤 {fmt.name_of(state.me)} (`{state.me.id}`)\n"
        f"⌨️ префикс `{config.PREFIX}` · команд {len(dispatcher.COMMANDS)}\n"
        f"🔇 мутов восстановлено: {len(state.mutes)}\n"
        f"📨 отчёты идут {where}"
    )

    try:
        await client.run_until_disconnected()
    finally:
        task.cancel()
        if runner is not None:
            await runner.cleanup()
        if state.bot is not None:
            await state.bot.disconnect()
        await db.close()


def main() -> None:
    setup_logging()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("остановлен вручную")


if __name__ == "__main__":
    main()
