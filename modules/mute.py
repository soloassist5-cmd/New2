"""Мут собеседника: его сообщения удаляются у всех сразу после отправки.

`.mute` в личке превращает само сообщение команды в уведомление с анимацией
набора текста — собеседник видит именно его.
"""
from __future__ import annotations

import asyncio
import logging
import time

from telethon import events
from telethon.errors import ChatAdminRequiredError, MessageDeleteForbiddenError

import config
import db
from core import anim, fmt, mediastore, reporter, state
from core.dispatcher import Ctx, command

log = logging.getLogger("mute")

CAT = "Мут"

MUTED_FOREVER_SELF = "🔇 **Вы были замучены на неопределённый срок.**"
MUTED_TIMED_SELF = "🔇 **Вы были замучены на {period}.**"
MUTED_FOREVER_OTHER = "🔇 **{who} замучен(а) на неопределённый срок.**"
MUTED_TIMED_OTHER = "🔇 **{who} замучен(а) на {period}.**"
UNMUTED_SELF = "🔊 **С вас снят мут. Можете писать.**"
UNMUTED_OTHER = "🔊 **С {who} снят мут.**"


def _final_text(*, is_peer: bool, who: str, seconds: int, global_: bool = False) -> str:
    if seconds:
        period = fmt.human_delta(seconds)
        text = (MUTED_TIMED_SELF if is_peer else MUTED_TIMED_OTHER).format(
            who=who, period=period)
    else:
        text = (MUTED_FOREVER_SELF if is_peer else MUTED_FOREVER_OTHER).format(who=who)
    return text + ("\n_(во всех чатах)_" if global_ else "")


async def _do_mute(ctx: Ctx, *, global_: bool) -> None:
    user_id, entity, rest = await ctx.resolve_target()
    if user_id is None:
        await ctx.fail("Кого мутим? Ответьте на сообщение или укажите @username / id.")
        return
    if state.me is not None and user_id == state.me.id:
        await ctx.fail("Себя мутить бессмысленно.")
        return

    seconds = fmt.parse_duration(rest[0]) if rest else None
    reason_parts = rest[1:] if (rest and seconds is not None) else rest
    reason = " ".join(reason_parts)
    seconds = seconds or 0
    until = int(time.time()) + seconds if seconds else 0

    scope = 0 if global_ else ctx.chat_id
    await state.mute_user(scope, user_id, until, reason)

    is_peer = ctx.is_private and user_id == ctx.chat_id
    who = fmt.mention(entity, user_id)
    final = _final_text(is_peer=is_peer, who=who, seconds=seconds, global_=global_)
    if reason:
        final += f"\n_Причина: {reason}_"

    style = "off" if {"q", "quiet"} & ctx.flags else None
    await anim.play(ctx.msg, final, icon="🔇", style=style)


@command("mute", args="[@user|reply] [10m|2h|1d] [причина]", cat=CAT,
         desc="замутить в этом чате: все его новые сообщения удаляются у всех")
async def cmd_mute(ctx: Ctx) -> None:
    await _do_mute(ctx, global_=False)


@command("gmute", args="[@user|reply] [срок] [причина]", cat=CAT,
         desc="то же самое, но во всех чатах сразу")
async def cmd_gmute(ctx: Ctx) -> None:
    await _do_mute(ctx, global_=True)


async def _do_unmute(ctx: Ctx, *, global_: bool) -> None:
    user_id, entity, _ = await ctx.resolve_target()
    if user_id is None:
        await ctx.fail("Кого размутить? Ответьте на сообщение или укажите @username / id.")
        return

    scope = 0 if global_ else ctx.chat_id
    existed = await state.unmute_user(scope, user_id)
    if not existed and not global_:
        existed = await state.unmute_user(0, user_id)   # вдруг был глобальный
    if not existed:
        await ctx.fail("Этот пользователь и не был замучен.")
        return

    is_peer = ctx.is_private and user_id == ctx.chat_id
    final = (UNMUTED_SELF if is_peer
             else UNMUTED_OTHER.format(who=fmt.mention(entity, user_id)))
    style = "off" if {"q", "quiet"} & ctx.flags else None
    await anim.play(ctx.msg, final, icon="🔊", style=style)


@command("unmute", args="[@user|reply]", cat=CAT, desc="снять мут в этом чате")
async def cmd_unmute(ctx: Ctx) -> None:
    await _do_unmute(ctx, global_=False)


@command("ungmute", args="[@user|reply]", cat=CAT, desc="снять глобальный мут")
async def cmd_ungmute(ctx: Ctx) -> None:
    await _do_unmute(ctx, global_=True)


@command("mutelist", args="", cat=CAT, desc="список активных мутов", aliases=["mutes"])
async def cmd_mutelist(ctx: Ctx) -> None:
    rows = await db.all_mutes()
    if not rows:
        await ctx.done("🔇 Список мутов пуст.")
        return

    now = int(time.time())
    lines = ["🔇 **Активные муты**", ""]
    for row in rows:
        if row["until"] and row["until"] <= now:
            continue
        try:
            entity = await ctx.client.get_entity(row["user_id"])
            who = fmt.mention(entity, row["user_id"])
        except Exception:
            who = f"`{row['user_id']}`"
        scope = "везде" if row["chat_id"] == 0 else f"чат `{row['chat_id']}`"
        left = ("бессрочно" if not row["until"]
                else f"ещё {fmt.human_delta(row['until'] - now)}")
        line = f"• {who} — {scope}, {left}"
        if row["reason"]:
            line += f"\n  _{row['reason']}_"
        lines.append(line)
    await ctx.done(fmt.truncate("\n".join(lines), 4000))


# --------------------------------------------------------------- перехват ---

async def _log_intercepted(event, media_ref: int | None) -> None:
    if not config.MUTE_LOG:
        return
    try:
        sender = await event.get_sender()
        who = fmt.mention(sender, event.sender_id)
    except Exception:
        who = f"`{event.sender_id}`"
    kind = mediastore.media_kind(event.message)
    head = f"🔇 **Перехвачено у замученного** {who}"
    body = fmt.truncate(event.message.message or "", 2000)
    if kind:
        head += f"\n{mediastore.KIND_ICON.get(kind, '📎')} {kind}"
    text = head + (f"\n\n{body}" if body else "")
    await reporter.send_report(text, media_ref=media_ref)


async def _enforce(event) -> None:
    """Удаляет сообщение замученного у всех — до того, как его успеют прочитать."""
    if event.out or not event.sender_id:
        return
    if not await state.is_muted(event.chat_id, event.sender_id):
        return

    message = event.message
    snap_task = None
    if config.MUTE_LOG and getattr(message, "media", None) is not None:
        snap_task = asyncio.create_task(mediastore.snapshot(message))

    try:
        await event.client.delete_messages(await event.get_input_chat(),
                                           [message.id], revoke=True)
        state.mark_own_deletion(event.chat_id, message.id,
                                private=bool(event.is_private))
    except (ChatAdminRequiredError, MessageDeleteForbiddenError):
        await reporter.send_report(
            f"⚠️ Не хватает прав удалять сообщения в чате `{event.chat_id}` — "
            f"мут там не работает.")
        return
    except Exception as e:                                   # noqa: BLE001
        log.warning("не удалось удалить сообщение замученного: %r", e)
        return

    media_ref = None
    if snap_task is not None:
        try:
            media_ref = await snap_task
        except Exception:
            media_ref = None
    await _log_intercepted(event, media_ref)


def setup(client) -> None:
    client.add_event_handler(_enforce, events.NewMessage(incoming=True))
