"""Mini App: проверка подписи Telegram и API для страницы.

Страница открывается внутри Telegram и присылает `initData` — строку, которую
Telegram подписал ботовым токеном. Из неё и только из неё берётся, кто зашёл:
любой id, пришедший из тела запроса, — это то, что прислал браузер, а его
пишет кто угодно.

Подпись считается ровно по спецификации: ключ — HMAC("WebAppData", токен),
им подписывается строка из пар `ключ=значение`, отсортированных по ключу, без
самого `hash`. Плюс срок годности: подписанная строка не протухает сама, и без
проверки времени украденная один раз работала бы вечно.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from urllib.parse import parse_qsl

import config
import db
from bot import cleanup, dossier
from core import chatprefs, state

log = logging.getLogger("webapp")

MAX_AGE = 24 * 3600          # сутки: дольше живущая подпись — лишний риск
LIST_LIMIT = 50


class Denied(Exception):
    """Запрос не прошёл проверку. Наружу уходит только код, без подробностей."""

    def __init__(self, reason: str, status: int = 403):
        super().__init__(reason)
        self.status = status


def check(init_data: str, *, token: str | None = None, now: int | None = None) -> dict:
    """Возвращает поле `user` из подписанных данных. Иначе — Denied."""
    token = token or config.BOT_TOKEN
    if not token:
        raise Denied("бот без токена", 503)
    if not init_data:
        raise Denied("нет initData")

    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    fields = dict(pairs)
    given = fields.pop("hash", "")
    if not given:
        raise Denied("нет подписи")

    check_string = "\n".join(f"{key}={value}"
                             for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, given):
        raise Denied("подпись не сходится")

    try:
        signed_at = int(fields.get("auth_date", 0))
    except ValueError:
        signed_at = 0
    if not signed_at or (now or int(time.time())) - signed_at > MAX_AGE:
        raise Denied("подпись просрочена")

    try:
        user = json.loads(fields.get("user") or "{}")
    except json.JSONDecodeError:
        user = {}
    if not user.get("id"):
        raise Denied("в подписи нет пользователя")
    return user


async def owner_of(init_data: str) -> tuple[int, dict]:
    """Кто зашёл. Неодобренного дальше не пускаем — бот хранит переписку."""
    user = check(init_data)
    owner_id = int(user["id"])
    if not state.is_approved(owner_id):
        raise Denied("доступ не открыт")
    return owner_id, user


# ------------------------------------------------------------------ данные --

def _row(row, keys) -> dict:
    return {key: row[key] for key in keys}


async def snapshot(owner_id: int) -> dict:
    """Всё, что страница показывает на первом экране."""
    stats = await db.stats(owner_id)
    counts = await db.counts_by_kind(owner_id)
    connection_id = state.business_of(owner_id)
    return {
        "connected": bool(connection_id),
        "rights": state.rights_of(connection_id) if connection_id else {},
        "dnd": state.dnd_active(owner_id),
        "dnd_since": state.dnd_since(owner_id),
        "admin": state.is_admin(owner_id),
        "prefix": config.PREFIX,
        "stats": {"deleted": stats["deleted"], "intercepted": stats["intercepted"],
                  "edits": stats["edits"], "cached": stats["cached"],
                  "mutes": stats["mutes"]},
        "counts": counts,
        "kinds": [{"key": key, "icon": icon, "name": name, "hint": hint}
                  for key, icon, name, hint in cleanup.KINDS],
    }


async def listing(owner_id: int, what: str, limit: int) -> list[dict]:
    limit = max(1, min(limit or 20, LIST_LIMIT))
    if what == "deleted":
        rows = await db.last_deleted(owner_id, None, limit)
        return [_row(row, ("chat_id", "user_name", "text", "media_type", "date",
                           "deleted_at")) for row in rows]
    if what == "intercepted":
        rows = await db.intercepted(owner_id, limit=limit)
        return [_row(row, ("chat_id", "user_name", "text", "media_type", "date",
                           "reason")) for row in rows]
    if what == "urgent":
        rows = await db.urgent_calls(owner_id, limit=limit)
        return [_row(row, ("user_id", "user_name", "chat_id", "reason", "at"))
                for row in rows]
    if what == "mutes":
        rows = await db.all_mutes(owner_id)
        return [_row(row, ("chat_id", "user_id", "until", "reason", "created_at"))
                for row in rows[:limit]]
    if what == "allowed":
        rows = await db.allowed_users(owner_id)
        return [_row(row, ("user_id", "name", "added_at")) for row in rows[:limit]]
    raise Denied("неизвестный список", 400)


# ---------------------------------------------------------------- действия --

async def act(owner_id: int, action: str, payload: dict) -> dict:
    """Действия страницы. owner_id приходит из подписи, а не из тела запроса."""
    if action == "dnd":
        if payload.get("on"):
            await state.set_dnd(owner_id)
        else:
            await state.clear_dnd(owner_id)
        return {"dnd": state.dnd_active(owner_id)}

    if action == "unmute":
        user_id = _as_id(payload.get("user_id"))
        chat_id = _as_id(payload.get("chat_id"), default=0)
        await state.unmute_user(owner_id, chat_id, user_id)
        return {"ok": True}

    if action in {"allow", "deny"}:
        user_id = _as_id(payload.get("user_id"))
        if action == "allow":
            await state.allow_user(owner_id, user_id,
                                   await db.name_of(owner_id, user_id))
        else:
            await state.deny_user(owner_id, user_id)
        return {"ok": True}

    if action == "clear":
        kind = payload.get("kind") or ""
        if kind != cleanup.ALL and kind not in db.PURGEABLE:
            raise Denied("неизвестная область", 400)
        code = cleanup.ALL if kind == cleanup.ALL else cleanup.CODES[kind]
        scope = payload.get("scope") or "a"
        if scope not in {"a", "d"}:
            raise Denied("неизвестная область чистки", 400)
        value = _as_id(payload.get("value"), default=0)
        removed = await cleanup.apply(owner_id, code, scope, value)
        return {"removed": removed}

    if action == "dossier":
        user_id = _as_id(payload.get("user_id"))
        return {"card": await dossier.card(state.api, owner_id, user_id)}

    if action == "chat":
        chat_id = _as_id(payload.get("chat_id"))
        key = payload.get("key") or ""
        if key not in {"antidelete", "log_edits", "ignored"}:
            raise Denied("неизвестная настройка", 400)
        await chatprefs.toggle(owner_id, chat_id, key, bool(payload.get("on")))
        return {"ok": True}

    raise Denied("неизвестное действие", 400)


def _as_id(value, *, default: int | None = None) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        if default is None:
            raise Denied("нужен числовой id", 400) from None
        return default
