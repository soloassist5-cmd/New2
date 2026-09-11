"""Анимация редактирования собственного сообщения.

Telegram ограничивает частоту правок, поэтому:
  * число кадров фиксировано и невелико;
  * FloodWait длиннее FLOOD_LIMIT прерывает анимацию и сразу ставит финальный текст;
  * MessageNotModified игнорируется.

Про ритм. Раньше пауза отсчитывалась *после* ответа Telegram, поэтому кадр
менялся раз в delay + время правки. Правка живёт от 0.1 до 0.9 с в зависимости
от связи, так что заказанные 0.6 с превращались то в 0.7, то в 1.5 — анимация
и дёргалась, и тянулась. Здесь пауза считается так, чтобы **кадры сменялись
ровно раз в delay**: из неё вычитается время, которое правка занимала в прошлый
раз. А если связь медленнее заказанного темпа, темп подстраивается под неё —
гнать кадры быстрее, чем их доставляет Telegram, значит копить правки в очереди
и получить их пачкой.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from telethon.errors import (
    FloodWaitError,
    MessageIdInvalidError,
    MessageNotModifiedError,
)

import config

log = logging.getLogger("anim")

FLOOD_LIMIT = 5          # сек: дольше — не ждём, обрываем анимацию
MAX_TYPE_FRAMES = 10     # больше правок — выше риск словить флуд-лимит
FILLED, EMPTY = "▰", "▱"
CURSOR = "▌"

MIN_FRAME = 0.35         # чаще Telegram всё равно не успевает перерисовать
MAX_FRAME = 2.0          # одна медленная правка не должна растянуть всю анимацию
MIN_GAP = 0.05           # не отправлять правку вплотную к предыдущей
PACE_FACTOR = 1.15       # запас поверх реального времени правки
EMA = 0.5                # насколько свежая правка влияет на оценку скорости связи
SETTLE = 0.35            # пауза после отправки: иначе клиент склеит её с правкой

# Незакрытая разметка в промежуточном кадре видна как мусор, поэтому её убираем:
# сначала ссылки [текст](url) -> текст, затем сами маркеры.
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD = re.compile(r"\*\*|~~|`|(?<!\w)_+|_+(?!\w)")


def strip_md(text: str) -> str:
    return _MD.sub("", _MD_LINK.sub(r"\1", text))


def _chunks(words: list[str], limit: int) -> list[list[str]]:
    """Режет слова на не более чем limit групп, чтобы не упереться в лимит правок."""
    if len(words) <= limit:
        return [[w] for w in words]
    size = -(-len(words) // limit)
    return [words[i:i + size] for i in range(0, len(words), size)]


class Aborted(Exception):
    """Анимацию продолжать нельзя (флуд-лимит или сообщение исчезло)."""


async def safe_edit(msg, text: str, *, link_preview: bool = False):
    """Правка с проглатыванием штатных ошибок. Возвращает сообщение или None."""
    try:
        return await msg.edit(text, link_preview=link_preview)
    except MessageNotModifiedError:
        return msg
    except FloodWaitError as e:
        if e.seconds > FLOOD_LIMIT:
            raise Aborted from e
        await asyncio.sleep(e.seconds + 0.5)
        try:
            return await msg.edit(text, link_preview=link_preview)
        except Exception as retry_error:
            raise Aborted from retry_error
    except MessageIdInvalidError as e:
        raise Aborted from e
    except Exception as e:              # noqa: BLE001 — анимация не должна ронять команду
        log.warning("edit failed: %r", e)
        raise Aborted from e


async def safe_edit_quiet(msg, text: str) -> None:
    """Последняя попытка поставить текст: результат важнее спецэффекта."""
    try:
        await msg.edit(text, link_preview=False)
    except Exception:                                        # noqa: BLE001
        pass


def _pace(delay: float, spent: float = 0.0) -> float:
    """Шаг анимации: не быстрее, чем рисует Telegram, и не быстрее, чем просили."""
    if delay <= 0:
        return 0.0                       # анимация выключена (и так же в тестах)
    return min(max(delay, MIN_FRAME, spent * PACE_FACTOR), MAX_FRAME)


def _dedupe(frames: list[str]) -> list[str]:
    """Повтор кадра — это правка «ни во что»: лишняя пауза и лишний запрос."""
    out: list[str] = []
    for frame in frames:
        if not out or out[-1] != frame:
            out.append(frame)
    return out


async def play_frames(msg, frames: list[str], *, delay: float | None = None,
                      settle: float = 0.0) -> None:
    """Проигрывает кадры с ровным шагом; последний кадр показывается всегда."""
    delay = config.ANIM_DELAY if delay is None else delay
    frames = _dedupe(frames)
    if not frames:
        return
    if delay <= 0:                       # анимация выключена — только кадры
        for frame in frames:
            await safe_edit(msg, frame)
        return

    if settle:
        await asyncio.sleep(min(settle, delay))

    cost = 0.0                           # во сколько обходится одна правка
    for i, frame in enumerate(frames):
        if i:
            # Пауза = нужный шаг минус то, что съест сама правка. Тогда кадр
            # у собеседника меняется ровно раз в шаг, а не раз в шаг + связь.
            await asyncio.sleep(max(_pace(delay, cost) - cost, MIN_GAP))
        started = time.monotonic()
        await safe_edit(msg, frame)
        spent = time.monotonic() - started
        # Вверх оценка идёт сразу, вниз — постепенно: связь, которая один раз
        # тормознула, обычно тормозит и дальше, и лучше сразу сбавить темп,
        # чем ловить рывок на каждом втором кадре.
        cost = spent if spent > cost else cost * (1 - EMA) + spent * EMA


def bar(step: int, steps: int, width: int = 8) -> str:
    filled = round(width * step / steps)
    return FILLED * filled + EMPTY * (width - filled)


def bar_frames(icon: str, steps: int = 4, width: int = 8) -> list[str]:
    return [f"{icon} {bar(step, steps, width)}" for step in range(steps + 1)]


def type_frames(final: str, *, prefix: str = "", cursor: bool = True) -> list[str]:
    """Кадры пословного печатания; разметка появляется только на финальном."""
    groups = _chunks(strip_md(final).split(" "), MAX_TYPE_FRAMES)
    frames, shown = [], ""
    for i, group in enumerate(groups):
        shown = f"{shown} {' '.join(group)}".strip()
        tail = f" {CURSOR}" if cursor and i < len(groups) - 1 else ""
        frames.append(f"{prefix}{shown}{tail}")
    frames.append(f"{prefix}{final}")
    return frames


async def loading(msg, final: str, *, icon: str = "🔇", steps: int = 4,
                  delay: float | None = None, settle: float = SETTLE) -> None:
    """Полоса загрузки, затем финальный текст."""
    await play_frames(msg, bar_frames(icon, steps) + [final],
                      delay=delay, settle=settle)


async def type_out(msg, final: str, *, prefix: str = "", delay: float | None = None,
                   cursor: bool = True, settle: float = SETTLE) -> None:
    """Пословное «печатание» текста; разметка появляется только на финальном кадре."""
    await play_frames(msg, type_frames(final, prefix=prefix, cursor=cursor),
                      delay=delay, settle=settle)


async def play(msg, final: str, *, icon: str = "🔇", style: str | None = None,
               delay: float | None = None) -> None:
    """Проигрывает анимацию выбранного стиля и гарантированно ставит финальный текст.

    style: 'type' — полоса + пословное печатание, 'bar' — только полоса, 'off' — без анимации.
    """
    style = (style or config.MUTE_ANIM).lower()
    delay = config.ANIM_DELAY if delay is None else delay
    try:
        if style == "off":
            await safe_edit(msg, final)
            return
        if style == "bar":
            await loading(msg, final, icon=icon, delay=delay)
            return
        # style == "type": полоса и печатание — одна непрерывная анимация,
        # поэтому и ритм у них общий, без паузы на стыке.
        frames = bar_frames(icon, 3, width=6)[:3] + type_frames(final)
        await play_frames(msg, frames, delay=delay, settle=SETTLE)
    except Aborted:
        # Анимация сорвалась — важен результат, а не спецэффект.
        try:
            await msg.edit(final, link_preview=False)
        except Exception:
            pass
