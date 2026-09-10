"""Мини-HTTP-сервер для healthcheck.

Бесплатные тарифы Render/Koyeb принимают только сервисы, слушающие $PORT,
и пингуют их, чтобы контейнер не засыпал.
"""
from __future__ import annotations

import logging
import time

from aiohttp import web

import config
from core import state

log = logging.getLogger("health")


async def _status(_request: web.Request) -> web.Response:
    connected = bool(state.api) or bool(state.client and state.client.is_connected())
    return web.json_response({
        "status": "ok" if connected else "starting",
        "connected": connected,
        "uptime_sec": int(time.time() - state.start_time),
        "mutes": len(state.mutes),
        "afk": state.afk_since is not None,
        "bot": bool(state.api),
        "bot_reachable": bool(state.api) and not state.bot_blocked,
        "business_connections": sum(1 for i in state.business.values()
                                    if i.get("is_enabled")),
        "userbot": bool(state.client),
    }, status=200 if connected else 503)


async def start() -> web.AppRunner | None:
    if not config.PORT:
        return None
    app = web.Application()
    app.router.add_get("/", _status)
    app.router.add_get("/health", _status)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PORT)
    await site.start()
    log.info("healthcheck слушает 0.0.0.0:%s", config.PORT)
    return runner
