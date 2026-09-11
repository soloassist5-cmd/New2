"""Необязательные команды на каждый день: стикеры, кубики, мелкая анимация.

Работают в переписке и видны собеседнику — в этом весь смысл.
"""
from __future__ import annotations

import asyncio
import logging

from bot import fun
from bot.dotcmd import BizCtx, bizcmd
from bot.editable import BizMessage
from core import anim, fmt

log = logging.getLogger("funcmd")

CAT = "Развлечения"
CAT_DELAY = 0.6
SPEED = 1.0          # множитель пауз; тесты выставляют 0, чтобы не ждать

# Стикеры, у которых есть своя короткая команда. Всё остальное — через `.e`.
STICKERS = {
    "rose": ("🌹", "роза"),
    "heart": ("❤️", "сердце"),
    "fire": ("🔥", "огонь"),
    "cat": ("🐱", "кот"),
    "party": ("🎉", "праздник"),
    "kiss": ("😘", "поцелуй"),
    "cry": ("😭", "слёзы"),
    "think": ("🤔", "задумчивость"),
    "ok": ("👌", "окей"),
    "clown": ("🤡", "клоун"),
}

# Кубики Telegram: анимацию рисует сам мессенджер.
DICE = {
    "dice": ("🎲", "кубик"),
    "dart": ("🎯", "дартс"),
    "basket": ("🏀", "баскетбол"),
    "foot": ("⚽", "футбол"),
    "bowl": ("🎳", "боулинг"),
    "slot": ("🎰", "однорукий бандит"),
}

KEYCAPS = ("0️⃣", "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣",
           "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟")


async def _post(ctx: BizCtx, text: str) -> BizMessage | None:
    """Убирает команду и ставит на её место сообщение, которое можно править."""
    await ctx.drop_command()
    try:
        sent = await ctx.api.send_message(ctx.chat_id, text,
                                          business_connection_id=ctx.connection_id)
    except Exception as e:                                   # noqa: BLE001
        log.warning("не удалось отправить: %r", e)
        return None
    return BizMessage(ctx.api, ctx.chat_id, sent["message_id"], ctx.connection_id)


async def _animate(ctx: BizCtx, frames: list[str], *, delay: float = CAT_DELAY) -> None:
    message = await _post(ctx, frames[0])
    if message is None:
        return
    try:
        for frame in frames[1:]:
            await asyncio.sleep(delay * SPEED)
            await anim.safe_edit(message, frame)
    except anim.Aborted:
        await anim.safe_edit_quiet(message, frames[-1])


# ----------------------------------------------------------------- стикеры --

async def _sticker(ctx: BizCtx, emoji: str) -> None:
    await ctx.drop_command()
    if not await fun.send_sticker(ctx.api, ctx.chat_id, emoji, ctx.connection_id):
        await ctx.private(f"Не получилось нарисовать {emoji} — "
                          "похоже, на сервере нет шрифта цветных эмодзи.")


def _register_stickers() -> None:
    for name, (emoji, label) in STICKERS.items():
        def handler(ctx: BizCtx, emoji: str = emoji) -> object:
            return _sticker(ctx, emoji)

        bizcmd(name, visible=True, cat=CAT,
               desc=f"{emoji} {label} стикером")(handler)


_register_stickers()


@bizcmd("e", args="<эмодзи>", visible=True, aliases=["sticker"], cat=CAT,
        desc="любой эмодзи стикером, без фона")
async def cmd_emoji(ctx: BizCtx) -> None:
    raw = ctx.raw.strip()
    if not fun.is_emoji(raw):
        await ctx.fail("Нужен один эмодзи: `.e 🦊`")
        return
    await _sticker(ctx, raw)


# ------------------------------------------------------------- кубики -------

def _register_dice() -> None:
    for name, (emoji, label) in DICE.items():
        async def handler(ctx: BizCtx, emoji: str = emoji) -> None:
            await ctx.drop_command()
            try:
                await ctx.api.send_dice(ctx.chat_id, emoji,
                                        business_connection_id=ctx.connection_id)
            except Exception as e:                           # noqa: BLE001
                log.warning("кубик не бросился: %r", e)

        bizcmd(name, visible=True, cat=CAT,
               desc=f"{emoji} {label}, анимацию рисует Telegram")(handler)


_register_dice()


# ---------------------------------------------------------- анимация --------

@bizcmd("type", args="<текст>", visible=True, cat=CAT,
        desc="напечатать текст по словам, с курсором")
async def cmd_type(ctx: BizCtx) -> None:
    text = ctx.raw.strip()
    if not text:
        await ctx.fail("Что печатать? `.type привет`")
        return
    message = await _post(ctx, anim.CURSOR)
    if message is None:
        return
    try:
        await anim.type_out(message, text)
    except anim.Aborted:
        await anim.safe_edit_quiet(message, text)


@bizcmd("countdown", args="[N]", visible=True, aliases=["cd"], cat=CAT,
        desc="обратный отсчёт и «поехали»")
async def cmd_countdown(ctx: BizCtx) -> None:
    start = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 3
    start = max(1, min(start, 10))
    frames = [KEYCAPS[n] for n in range(start, 0, -1)] + ["🚀 **Поехали!**"]
    await _animate(ctx, frames, delay=1.0)


@bizcmd("love", visible=True, cat=CAT, desc="растущее сердце")
async def cmd_love(ctx: BizCtx) -> None:
    await _animate(ctx, ["🤍", "💗", "💖", "💝", "❤️‍🔥"], delay=0.5)


@bizcmd("flip", visible=True, aliases=["coin"], cat=CAT, desc="подбросить монетку")
async def cmd_flip(ctx: BizCtx) -> None:
    result = fun.coin()
    await _animate(ctx, ["🪙 …", "🪙 ⟳", "🪙 …", f"🪙 **{result}!**"], delay=0.4)


# -------------------------------------------------------- случайности -------

@bizcmd("roll", args="[2d6]", visible=True, cat=CAT, desc="бросить кубики")
async def cmd_roll(ctx: BizCtx) -> None:
    thrown = fun.roll(ctx.raw or "1d6")
    if thrown is None:
        await ctx.fail("Не понял запись. Так: `.roll 2d6` — два шестигранных.")
        return
    throws, total, sides = thrown
    if len(throws) == 1:
        await _post(ctx, f"🎲 **{total}** _(d{sides})_")
        return
    parts = " + ".join(str(value) for value in throws)
    await _post(ctx, f"🎲 {parts} = **{total}** _({len(throws)}d{sides})_")


@bizcmd("8ball", args="<вопрос>", visible=True, aliases=["ball"], cat=CAT,
        desc="волшебный шар отвечает на вопрос")
async def cmd_eight_ball(ctx: BizCtx) -> None:
    question = fmt.truncate(ctx.raw.strip(), 200)
    if not question:
        await ctx.fail("А вопрос? `.8ball полетим в отпуск?`")
        return
    await _animate(ctx, ["🎱 …", "🎱 ⟳", f"🎱 _{question}_\n**{fun.eight_ball()}**"],
                   delay=0.7)


@bizcmd("choose", args="а | б | в", visible=True, aliases=["pick"], cat=CAT,
        desc="выбрать одно из перечисленного")
async def cmd_choose(ctx: BizCtx) -> None:
    picked = fun.choose(ctx.raw)
    if picked is None:
        await ctx.fail("Дайте хотя бы два варианта: `.choose чай | кофе`")
        return
    await _animate(ctx, ["🤔 …", "🤔 ⟳", f"👉 **{picked}**"], delay=0.6)
