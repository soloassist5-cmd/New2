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
INIT_HEADER = "X-Init-Data"
PAGE = config.ROOT / "miniapp" / "index.html"


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


# ------------------------------------------------------------------ Mini App --

async def _page(_request: web.Request) -> web.Response:
    """Сама страница. Открывается только внутри Telegram — там и проверка."""
    try:
        body = PAGE.read_text(encoding="utf-8")
    except OSError as e:
        log.error("страница приложения не читается: %r", e)
        return web.Response(status=500, text="no page")
    return web.Response(text=body, content_type="text/html", charset="utf-8",
                        headers={"Cache-Control": "no-cache"})


async def _api(request: web.Request) -> web.Response:
    """Всё, что делает страница. Кто зашёл — только из подписи Telegram."""
    from bot import webapp

    try:
        owner_id, _ = await webapp.owner_of(request.headers.get(INIT_HEADER, ""))
    except webapp.Denied as e:
        # Наружу — только код: по разнице формулировок удобно подбирать подпись.
        log.info("приложение: отказ (%s) от %s", e, request.remote)
        return web.json_response({"error": "denied"}, status=e.status)

    try:
        payload = await request.json() if request.can_read_body else {}
    except Exception:                                        # noqa: BLE001
        return web.json_response({"error": "bad json"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"error": "bad json"}, status=400)

    what = request.match_info["what"]
    try:
        if what == "state":
            return web.json_response(await webapp.snapshot(owner_id))
        if what == "list":
            items = await webapp.listing(owner_id, payload.get("what", ""),
                                         payload.get("limit", 20))
            return web.json_response({"items": items})
        if what == "act":
            return web.json_response(
                await webapp.act(owner_id, payload.get("action", ""), payload))
    except webapp.Denied as e:
        return web.json_response({"error": "denied"}, status=e.status)
    except Exception:                                        # noqa: BLE001
        log.exception("приложение: %s не отработало", what)
        return web.json_response({"error": "failed"}, status=500)
    return web.json_response({"error": "unknown"}, status=404)


async def start() -> web.AppRunner | None:
    if not config.PORT:
        return None
    app = web.Application()
    app.router.add_get("/", _status)
    app.router.add_get("/health", _status)
    if config.use_webhook():
        app.router.add_post(config.webhook_path(), _webhook)
    if config.webapp_url():
        app.router.add_get(config.WEBAPP_PATH, _page)
        app.router.add_post(config.WEBAPP_PATH + "/api/{what}", _api)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.PORT)
    await site.start()
    log.info("HTTP слушает 0.0.0.0:%s%s", config.PORT,
             f" (вебхук на {config.webhook_path()})" if config.use_webhook() else "")
    return runner
