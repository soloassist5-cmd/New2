"""Заметки и шаблоны ответов."""
from __future__ import annotations

import db
from core import fmt, state
from core.dispatcher import Ctx, command

CAT = "Заметки"


@command("save", args="<имя> [текст | реплаем]", cat=CAT,
         desc="сохранить заметку для этого чата")
async def cmd_save(ctx: Ctx) -> None:
    if not ctx.args:
        await ctx.fail("Укажите имя: `.save приветствие текст` или реплаем.")
        return
    name = ctx.args[0]
    content = ctx.raw.split(maxsplit=1)[1] if len(ctx.args) > 1 else ""
    if not content:
        reply = await ctx.reply_msg()
        content = (reply.message if reply else "") or ""
    if not content:
        await ctx.fail("Нечего сохранять — добавьте текст или ответьте на сообщение.")
        return
    await db.save_note(state.userbot_owner(), ctx.chat_id, name, content)
    await ctx.done(f"💾 Заметка `{name}` сохранена.", delete_after=6)


@command("get", args="<имя>", cat=CAT, desc="вставить заметку в чат")
async def cmd_get(ctx: Ctx) -> None:
    if not ctx.args:
        await ctx.fail("Укажите имя заметки.")
        return
    row = await db.get_note(state.userbot_owner(), ctx.chat_id, ctx.args[0])
    if row is None:
        await ctx.fail(f"Заметки `{ctx.args[0]}` нет.")
        return
    await ctx.edit(row["content"])


@command("notes", args="", cat=CAT, desc="список заметок чата")
async def cmd_notes(ctx: Ctx) -> None:
    rows = await db.list_notes(state.userbot_owner(), ctx.chat_id)
    if not rows:
        await ctx.done("📒 Заметок нет.", delete_after=8)
        return
    names = ", ".join(f"`{row['name']}`" for row in rows)
    await ctx.done(f"📒 **Заметки этого чата** ({len(rows)}):\n{fmt.truncate(names, 3500)}")


@command("delnote", args="<имя>", cat=CAT, desc="удалить заметку")
async def cmd_delnote(ctx: Ctx) -> None:
    if not ctx.args:
        await ctx.fail("Укажите имя заметки.")
        return
    await db.drop_note(state.userbot_owner(), ctx.chat_id, ctx.args[0])
    await ctx.done(f"🗑 Заметка `{ctx.args[0]}` удалена.", delete_after=6)
