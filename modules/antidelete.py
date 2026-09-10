"""Кэш сообщений + журнал удалённых и отредактированных."""
from __future__ import annotations

import asyncio
import io
import logging

from telethon import events

import config
import db
from core import fmt, mediastore, reporter, state
from core.dispatcher import Ctx, command

log = logging.getLogger("antidelete")

CAT = "Антиудаление"

_settings: dict[int, dict] = {}
_names: dict[int, str] = {}


def invalidate(chat_id: int) -> None:
    _settings.pop(chat_id, None)


def invalidate_all() -> None:
    """Сбрасывает кэш настроек — например, после восстановления базы."""
    _settings.clear()
    _names.clear()


async def flags(chat_id: int, is_private: bool) -> dict:
    cached = _settings.get(chat_id)
    if cached is not None:
        return cached
    row = await db.get_settings(chat_id)
    default_on = config.DEFAULT_ANTIDELETE and (is_private or config.ANTIDELETE_GROUPS)
    resolved = {
        "antidelete": bool(row["antidelete"]) if row["antidelete"] is not None else default_on,
        "log_edits": bool(row["log_edits"]) if row["log_edits"] is not None
        else (config.DEFAULT_LOG_EDITS and default_on),
        "save_media": bool(row["save_media"]) if row["save_media"] is not None
        else config.DEFAULT_SAVE_MEDIA,
        "ignored": bool(row["ignored"]),
    }
    _settings[chat_id] = resolved
    return resolved


async def _name(entity_id: int | None) -> str:
    if not entity_id:
        return "неизвестно"
    if entity_id in _names:
        return _names[entity_id]
    try:
        entity = await state.client.get_entity(entity_id)
        _names[entity_id] = fmt.name_of(entity)
    except Exception:
        _names[entity_id] = f"id {entity_id}"
    return _names[entity_id]


# ----------------------------------------------------------------- кэш ------

async def _snapshot_later(chat_id: int, msg_id: int, message) -> None:
    ref = await mediastore.snapshot(message)
    if ref:
        try:
            await db.set_media_ref(chat_id, msg_id, ref)
        except Exception as e:                               # noqa: BLE001
            log.debug("не записал media_ref: %r", e)


async def _cache(event) -> None:
    message = event.message
    if getattr(message, "action", None) is not None:
        return                                    # служебные сообщения не кэшируем
    text = message.message or ""
    if event.out and text.startswith(config.PREFIX):
        return                                    # свои же команды

    chat_id = event.chat_id
    conf = await flags(chat_id, bool(event.is_private))
    if conf["ignored"] or not conf["antidelete"]:
        return

    kind = mediastore.media_kind(message)
    try:
        await db.cache_message(
            chat_id=chat_id,
            msg_id=message.id,
            user_id=event.sender_id,
            is_private=bool(event.is_private),
            text=text,
            media_type=kind,
            media_ref=None,
            reply_to=getattr(message, "reply_to_msg_id", None),
            date=int(message.date.timestamp()) if message.date else db.now(),
        )
    except Exception as e:                                   # noqa: BLE001
        log.warning("кэш не записан: %r", e)
        return

    if kind and conf["save_media"]:
        asyncio.create_task(_snapshot_later(chat_id, message.id, message))


# ------------------------------------------------------------ удаления ------

async def _card(row) -> str:
    who = await _name(row["user_id"])
    where = await _name(row["chat_id"])
    lines = [
        "🗑 **Удалённое сообщение**",
        f"👤 {who} (`{row['user_id']}`)",
        f"💬 {where} (`{row['chat_id']}`)",
        f"🕒 отправлено {fmt.ts(row['date'])} · удалено {fmt.ts(db.now())}",
    ]
    if row["media_type"]:
        icon = mediastore.KIND_ICON.get(row["media_type"], "📎")
        lines.append(f"{icon} {row['media_type']}"
                     + ("" if row["media_ref"] else " _(копия не сохранилась)_"))
    text = row["text"]
    if text:
        lines += ["", fmt.truncate(text, 2500)]
    return "\n".join(lines)


async def _on_delete(event) -> None:
    chat_id = event.chat_id
    for msg_id in event.deleted_ids:
        if state.was_own_deletion(chat_id, msg_id):
            continue
        try:
            row = (await db.get_message(chat_id, msg_id)) if chat_id is not None else None
            if row is None:
                row = await db.find_private_message(msg_id)
            if row is None:
                continue
            if state.me is not None and row["user_id"] == state.me.id and not config.LOG_OWN:
                continue
            conf = await flags(row["chat_id"], bool(row["is_private"]))
            if not conf["antidelete"] or conf["ignored"]:
                continue
            await db.add_deleted(dict(row))
            await reporter.send_report(await _card(row), media_ref=row["media_ref"])
            await db.drop_message(row["chat_id"], row["msg_id"])
        except Exception as e:                               # noqa: BLE001
            log.warning("обработка удаления %s: %r", msg_id, e)


async def _on_edit(event) -> None:
    message = event.message
    chat_id = event.chat_id
    conf = await flags(chat_id, bool(event.is_private))
    if conf["ignored"] or not conf["log_edits"]:
        return
    row = await db.get_message(chat_id, message.id)
    new_text = message.message or ""
    if row is None:
        await _cache(event)
        return
    old_text = row["text"] or ""
    if old_text == new_text:
        return

    await db.add_edit(chat_id, message.id, event.sender_id, old_text, new_text)
    await db.cache_message(
        chat_id=chat_id, msg_id=message.id, user_id=row["user_id"],
        is_private=bool(row["is_private"]), text=new_text,
        media_type=row["media_type"], media_ref=row["media_ref"],
        reply_to=row["reply_to"], date=row["date"],
    )
    if state.me is not None and event.sender_id == state.me.id and not config.LOG_OWN:
        return
    who = await _name(event.sender_id)
    where = await _name(chat_id)
    await reporter.send_report(
        "✏️ **Сообщение изменено**\n"
        f"👤 {who} (`{event.sender_id}`)\n"
        f"💬 {where} (`{chat_id}`)\n\n"
        f"**Было:**\n{fmt.truncate(old_text, 1200)}\n\n"
        f"**Стало:**\n{fmt.truncate(new_text, 1200)}"
    )


# -------------------------------------------------------------- команды -----

def _on_off(args: list[str], current: bool) -> bool:
    if not args:
        return not current
    return args[0].lower() in {"on", "вкл", "1", "true", "да", "yes"}


async def _toggle(ctx: Ctx, key: str, label: str) -> None:
    conf = await flags(ctx.chat_id, ctx.is_private)
    value = _on_off(ctx.args, conf[key])
    await db.set_setting(ctx.chat_id, key, int(value))
    invalidate(ctx.chat_id)
    await ctx.done(f"{'✅' if value else '🚫'} {label}: **{'включено' if value else 'выключено'}** "
                   f"для этого чата.", delete_after=8)


@command("antidelete", args="[on|off]", cat=CAT, aliases=["ad"],
         desc="логировать удалённые сообщения этого чата")
async def cmd_antidelete(ctx: Ctx) -> None:
    await _toggle(ctx, "antidelete", "Антиудаление")


@command("logedits", args="[on|off]", cat=CAT,
         desc="логировать правки сообщений в этом чате")
async def cmd_logedits(ctx: Ctx) -> None:
    await _toggle(ctx, "log_edits", "Журнал правок")


@command("savemedia", args="[on|off]", cat=CAT,
         desc="сохранять копии медиа в лог-чат")
async def cmd_savemedia(ctx: Ctx) -> None:
    await _toggle(ctx, "save_media", "Сохранение медиа")


@command("ignore", args="[on|off]", cat=CAT,
         desc="полностью игнорировать этот чат")
async def cmd_ignore(ctx: Ctx) -> None:
    await _toggle(ctx, "ignored", "Игнорирование чата")


@command("deleted", args="[N]", cat=CAT, aliases=["dels"],
         desc="показать последние удалённые сообщения этого чата")
async def cmd_deleted(ctx: Ctx) -> None:
    limit = 10
    if ctx.args and ctx.args[0].isdigit():
        limit = max(1, min(int(ctx.args[0]), 30))
    scope = None if "all" in ctx.flags else ctx.chat_id
    rows = await db.last_deleted(scope, limit)
    if not rows:
        await ctx.done("🗑 Удалённых сообщений не найдено.")
        return
    out = [f"🗑 **Последние удалённые** ({len(rows)})", ""]
    for row in rows:
        who = await _name(row["user_id"])
        preview = fmt.truncate(row["text"] or "", 160) or (
            f"_{row['media_type']}_" if row["media_type"] else "_пусто_")
        out.append(f"• `{fmt.ts(row['deleted_at'])}` **{who}**\n  {preview}")
    await ctx.done(fmt.truncate("\n".join(out), 4000))


@command("restore", args="[N]", cat=CAT,
         desc="вернуть в чат N-е с конца удалённое сообщение (по умолчанию последнее)")
async def cmd_restore(ctx: Ctx) -> None:
    index = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 1
    rows = await db.last_deleted(ctx.chat_id, max(index, 1))
    if len(rows) < index:
        await ctx.fail("Столько удалённых сообщений не сохранено.")
        return
    row = rows[index - 1]
    who = await _name(row["user_id"])
    header = f"♻️ **Восстановлено** (от {who}, {fmt.ts(row['date'])})"
    body = row["text"] or ""
    if row["media_ref"] and state.log_entity is not None:
        try:
            source = await ctx.client.get_messages(state.log_entity, ids=row["media_ref"])
            if source is not None:
                await ctx.msg.delete()
                await ctx.client.send_file(ctx.chat_id, source.media,
                                           caption=f"{header}\n\n{body}"[:1024])
                return
        except Exception as e:                               # noqa: BLE001
            log.warning("не удалось приложить медиа: %r", e)
    await ctx.done(f"{header}\n\n{body}" if body else f"{header}\n\n_(только медиа)_")


@command("export", args="", cat=CAT,
         desc="выгрузить журнал удалённых этого чата файлом")
async def cmd_export(ctx: Ctx) -> None:
    from bot import transcript

    rows = await db.last_deleted(ctx.chat_id, 5000)
    if not rows:
        await ctx.fail("Нечего выгружать.")
        return
    # Юзербот имён в журнал не пишет — подставляем их здесь, по разрешённым id.
    ordered = [dict(row) for row in sorted(rows, key=lambda r: (r["date"], r["msg_id"]))]
    for row in ordered:
        if not row.get("user_name"):
            row["user_name"] = await _name(row["user_id"])
    when = db.now()
    payload, _ = transcript.build(
        ordered, chat_title=await _name(ctx.chat_id),
        owner_id=state.me.id if state.me else None,
        requested=len(ordered), when=when, reason="Журнал удалённых сообщений")

    data = io.BytesIO(payload)
    data.name = transcript.filename(ctx.chat_id, when)
    await ctx.msg.delete()
    await ctx.client.send_file(ctx.chat_id, data,
                               caption=f"🗑 Журнал удалённых: {len(ordered)} шт.")


def setup(client) -> None:
    client.add_event_handler(_cache, events.NewMessage())
    client.add_event_handler(_on_edit, events.MessageEdited())
    client.add_event_handler(_on_delete, events.MessageDeleted())
