"""HTTP-сервер сервиса: healthcheck и приём вебхука Telegram.

Бесплатные тарифы почти везде дают только web service — процесс обязан
слушать $PORT. Тот же сервер принимает апдейты от Telegram, если задан
WEBHOOK_URL, иначе бот работает через long polling.
"""
from __future__ import annotations

import logging
import time

from aiohttp import web

import config
from bot import poller
from core import state

log = logging.getLogger("web")

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


async def _status(_request: web.Request) -> web.Response:
    connected = bool(state.api) or bool(state.client and state.client.is_connected())
    return web.json_response({
        "status": "ok" if connected else "starting",
        "connected": connected,
        "uptime_sec": int(time.time() - state.start_time),
        "mutes": len(state.mutes),
        "afk": state.afk_since is not None,
        "transport": "webhook" if config.use_webhook() else "polling",
        "bot": bool(state.api),
        "bot_reachable": bool(state.api) and not state.bot_blocked,
        "business_connections": sum(1 for i in state.business.values()
                                    if i.get("is_enabled")),
        "userbot": bool(state.client),
    }, status=200 if connected else 503)


async def _webhook(request: web.Request) -> web.Response:
    """Апдейт от Telegram. Секрет проверяем до разбора тела."""
    if request.headers.get(SECRET_HEADER) != config.webhook_secret():
        log.warning("вебхук с чужим секретом от %s", request.remote)
        return web.Response(status=403, text="forbidden")
    try:
        update = await request.json()
    except Exception:                                        # noqa: BLE001
        return web.Response(status=400, text="bad json")

    try:
        await poller.dispatch(state.api, update)
    except Exception:                                        # noqa: BLE001
        # 200 в любом случае: иначе Telegram будет слать этот апдейт по кругу.
        log.exception("апдейт %s не обработан", update.get("update_id"))
    return web.Response(text="ok")


async def start() -> web.AppRunner | None:
    if not config.PORT:
        return None
    app = web.Application()
    app.router.add_get("/", _status)
    app.router.add_get("/health", _status)
    if config.use_webhook():
        app.router.add_post(config.webhook_path(), _webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PORT)
    await site.start()
    log.info("HTTP слушает 0.0.0.0:%s%s", config.PORT,
             f" (вебхук на {config.webhook_path()})" if config.use_webhook() else "")
    return runner
