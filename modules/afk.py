"""Режим «отошёл»: автоответ на личные сообщения и упоминания."""
from __future__ import annotations

import asyncio
import time

from telethon import events

import config
from core import anim, fmt, state
from core.dispatcher import Ctx, command

CAT = "AFK"
REPLY_COOLDOWN = 300      # не чаще раза в 5 минут на одного собеседника


@command("afk", args="[причина]", cat=CAT,
         desc="включить автоответ «отошёл»")
async def cmd_afk(ctx: Ctx) -> None:
    reason = ctx.raw.strip()
    state.set_afk(reason)
    text = "🌙 **Отошёл.**" + (f"\n_{reason}_" if reason else "")
    await anim.play(ctx.msg, text, icon="🌙", style="bar")


@command("unafk", args="", cat=CAT, desc="выключить автоответ вручную")
async def cmd_unafk(ctx: Ctx) -> None:
    if state.afk_since is None:
        await ctx.fail("Режим AFK и так выключен.")
        return
    away = time.time() - state.afk_since
    state.clear_afk()
    await ctx.done(f"☀️ **Я на месте.** Отсутствовал {fmt.uptime(away)}.", delete_after=10)


async def _autoreply(event) -> None:
    if state.afk_since is None or event.out or not event.sender_id:
        return
    mentioned = event.is_private or event.mentioned
    if not mentioned:
        return
    last = state.afk_replied.get(event.sender_id, 0)
    if time.time() - last < REPLY_COOLDOWN:
        return
    if await state.is_muted(state.userbot_owner(), event.chat_id, event.sender_id):
        return
    state.afk_replied[event.sender_id] = time.time()
    away = fmt.uptime(time.time() - state.afk_since)
    text = f"🌙 Меня нет уже {away}, отвечу позже."
    if state.afk_reason:
        text += f"\n_{state.afk_reason}_"
    try:
        await event.reply(text)
    except Exception:
        pass


async def _autoclear(event) -> None:
    """Любое своё обычное сообщение снимает AFK."""
    if state.afk_since is None:
        return
    if (event.message.message or "").startswith(config.PREFIX):
        return
    away = time.time() - state.afk_since
    state.clear_afk()
    try:
        note = await event.client.send_message(
            event.chat_id, f"☀️ **Я на месте.** Отсутствовал {fmt.uptime(away)}.")
        await asyncio.sleep(5)
        await note.delete()
    except Exception:
        pass


def setup(client) -> None:
    client.add_event_handler(_autoreply, events.NewMessage(incoming=True))
    client.add_event_handler(_autoclear, events.NewMessage(outgoing=True))
