"""Штатные админ-действия Telegram в группах (нужны права администратора)."""
from __future__ import annotations

import datetime as dt
import logging

from telethon.errors import ChatAdminRequiredError, UserAdminInvalidError
from telethon.tl.functions.channels import EditAdminRequest, EditBannedRequest
from telethon.tl.types import ChatAdminRights, ChatBannedRights

from core import fmt
from core.dispatcher import Ctx, command

log = logging.getLogger("admin")

CAT = "Админ (группы)"

BAN_ALL = ChatBannedRights(until_date=None, view_messages=True)
UNBAN = ChatBannedRights(until_date=None, view_messages=False, send_messages=False,
                         send_media=False, send_stickers=False, send_gifs=False,
                         send_games=False, send_inline=False, embed_links=False)
KICK = ChatBannedRights(until_date=dt.timedelta(seconds=60), view_messages=True)


def _restrict(seconds: int | None) -> ChatBannedRights:
    until = dt.timedelta(seconds=seconds) if seconds else None
    return ChatBannedRights(until_date=until, send_messages=True, send_media=True,
                            send_stickers=True, send_gifs=True, send_games=True,
                            send_inline=True, embed_links=True)


UNRESTRICT = ChatBannedRights(until_date=None, send_messages=False, send_media=False,
                              send_stickers=False, send_gifs=False, send_games=False,
                              send_inline=False, embed_links=False)


async def _apply(ctx: Ctx, rights: ChatBannedRights, ok: str) -> None:
    if ctx.is_private:
        await ctx.fail("Команда работает только в группах и каналах.")
        return
    user_id, entity, _ = await ctx.resolve_target()
    if user_id is None:
        await ctx.fail("Укажите пользователя реплаем, @username или id.")
        return
    try:
        await ctx.client(EditBannedRequest(ctx.chat_id, user_id, rights))
    except (ChatAdminRequiredError, UserAdminInvalidError):
        await ctx.fail("Недостаточно прав для этого действия.")
        return
    await ctx.done(ok.format(who=fmt.mention(entity, user_id)))


@command("ban", args="[@user|reply]", cat=CAT, desc="забанить в группе")
async def cmd_ban(ctx: Ctx) -> None:
    await _apply(ctx, BAN_ALL, "🚫 {who} забанен(а).")


@command("unban", args="[@user|reply]", cat=CAT, desc="снять бан")
async def cmd_unban(ctx: Ctx) -> None:
    await _apply(ctx, UNBAN, "✅ {who} разбанен(а).")


@command("kick", args="[@user|reply]", cat=CAT, desc="выгнать без бана")
async def cmd_kick(ctx: Ctx) -> None:
    await _apply(ctx, KICK, "👢 {who} исключён(а).")


@command("tmute", args="[@user|reply] [срок]", cat=CAT,
         desc="штатный мут Telegram (запрет писать), в отличие от .mute")
async def cmd_tmute(ctx: Ctx) -> None:
    _, _, rest = await ctx.resolve_target()
    seconds = fmt.parse_duration(rest[0]) if rest else None
    period = fmt.human_delta(seconds) if seconds else "неопределённый срок"
    await _apply(ctx, _restrict(seconds), "🔕 {who} лишён(а) права писать на " + period + ".")


@command("untmute", args="[@user|reply]", cat=CAT, desc="снять штатный мут")
async def cmd_untmute(ctx: Ctx) -> None:
    await _apply(ctx, UNRESTRICT, "🔔 {who} снова может писать.")


@command("promote", args="[@user|reply]", cat=CAT, desc="выдать права администратора")
async def cmd_promote(ctx: Ctx) -> None:
    if ctx.is_private:
        await ctx.fail("Только в группах.")
        return
    user_id, entity, _ = await ctx.resolve_target()
    if user_id is None:
        await ctx.fail("Укажите пользователя.")
        return
    rights = ChatAdminRights(change_info=True, delete_messages=True, ban_users=True,
                             invite_users=True, pin_messages=True, manage_call=True)
    try:
        await ctx.client(EditAdminRequest(ctx.chat_id, user_id, rights, "admin"))
    except Exception as e:                                   # noqa: BLE001
        await ctx.fail(f"Не вышло: `{type(e).__name__}`")
        return
    await ctx.done(f"⭐️ {fmt.mention(entity, user_id)} — администратор.")


@command("demote", args="[@user|reply]", cat=CAT, desc="снять права администратора")
async def cmd_demote(ctx: Ctx) -> None:
    if ctx.is_private:
        await ctx.fail("Только в группах.")
        return
    user_id, entity, _ = await ctx.resolve_target()
    if user_id is None:
        await ctx.fail("Укажите пользователя.")
        return
    empty = ChatAdminRights(change_info=False, delete_messages=False, ban_users=False,
                            invite_users=False, pin_messages=False, add_admins=False)
    try:
        await ctx.client(EditAdminRequest(ctx.chat_id, user_id, empty, ""))
    except Exception as e:                                   # noqa: BLE001
        await ctx.fail(f"Не вышло: `{type(e).__name__}`")
        return
    await ctx.done(f"➖ {fmt.mention(entity, user_id)} больше не администратор.")


@command("pin", args="(реплаем)", cat=CAT, desc="закрепить сообщение")
async def cmd_pin(ctx: Ctx) -> None:
    reply = await ctx.reply_msg()
    if reply is None:
        await ctx.fail("Ответьте на сообщение.")
        return
    await ctx.client.pin_message(ctx.chat_id, reply.id, notify="loud" in ctx.flags)
    await ctx.done("📌 Закреплено.", delete_after=5)


@command("unpin", args="(реплаем | -all)", cat=CAT, desc="открепить сообщение")
async def cmd_unpin(ctx: Ctx) -> None:
    if "all" in ctx.flags:
        await ctx.client.unpin_message(ctx.chat_id)
        await ctx.done("📌 Все открепления сделаны.", delete_after=5)
        return
    reply = await ctx.reply_msg()
    if reply is None:
        await ctx.fail("Ответьте на сообщение или используйте `-all`.")
        return
    await ctx.client.unpin_message(ctx.chat_id, reply.id)
    await ctx.done("📌 Откреплено.", delete_after=5)
