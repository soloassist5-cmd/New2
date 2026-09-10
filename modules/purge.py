"""Удаление и чистка сообщений."""
from __future__ import annotations

import asyncio
import logging

from core import fmt, state
from core.dispatcher import Ctx, command

log = logging.getLogger("purge")

CAT = "Чистка"
CHUNK = 100


async def _delete(ctx: Ctx, ids: list[int]) -> int:
    if not ids:
        return 0
    chat = await ctx.event.get_input_chat()
    removed = 0
    for start in range(0, len(ids), CHUNK):
        batch = ids[start:start + CHUNK]
        try:
            await ctx.client.delete_messages(chat, batch, revoke=True)
            state.mark_own_deletion(state.userbot_owner(), ctx.chat_id, *batch,
                                    private=ctx.is_private)
            removed += len(batch)
        except Exception as e:                               # noqa: BLE001
            log.warning("не удалось удалить пачку: %r", e)
    return removed


@command("del", args="(реплаем)", cat=CAT, aliases=["d"],
         desc="удалить сообщение, на которое отвечаете (у всех)")
async def cmd_del(ctx: Ctx) -> None:
    reply = await ctx.reply_msg()
    if reply is None:
        await ctx.fail("Ответьте на сообщение, которое нужно удалить.")
        return
    await _delete(ctx, [reply.id, ctx.msg.id])


@command("purge", args="(реплаем)", cat=CAT,
         desc="удалить всё от сообщения-реплая до текущего")
async def cmd_purge(ctx: Ctx) -> None:
    reply = await ctx.reply_msg()
    if reply is None:
        await ctx.fail("Ответьте на сообщение, с которого начинать чистку.")
        return
    ids = list(range(reply.id, ctx.msg.id + 1))
    removed = await _delete(ctx, ids)
    note = await ctx.client.send_message(ctx.chat_id, f"🧹 Удалено сообщений: **{removed}**")
    await asyncio.sleep(4)
    try:
        await note.delete()
    except Exception:
        pass


@command("purgeme", args="<N>", cat=CAT, aliases=["pm"],
         desc="удалить свои последние N сообщений в этом чате")
async def cmd_purgeme(ctx: Ctx) -> None:
    count = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 10
    count = max(1, min(count, 1000))
    ids = []
    async for message in ctx.client.iter_messages(ctx.chat_id, from_user="me",
                                                  limit=count + 1):
        ids.append(message.id)
    removed = await _delete(ctx, ids)
    note = await ctx.client.send_message(ctx.chat_id, f"🧹 Своих удалено: **{removed}**")
    await asyncio.sleep(4)
    try:
        await note.delete()
    except Exception:
        pass


@command("sd", args="<секунды> <текст>", cat=CAT,
         desc="отправить самоудаляющееся сообщение")
async def cmd_sd(ctx: Ctx) -> None:
    if len(ctx.args) < 2:
        await ctx.fail("Использование: `.sd 30 текст сообщения`")
        return
    seconds = fmt.parse_duration(ctx.args[0]) or 30
    text = ctx.raw.split(maxsplit=1)[1]
    msg = await ctx.edit(text)
    await asyncio.sleep(seconds)
    try:
        await msg.delete()
        state.mark_own_deletion(state.userbot_owner(), ctx.chat_id, msg.id,
                                private=ctx.is_private)
    except Exception:
        pass
