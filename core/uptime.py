"""Учёт запусков: как часто бот поднимается заново.

На бесплатных тарифах процесс живёт недолго — сервис засыпает без запросов,
пересоздаётся при обновлении, падает по памяти. Само по себе это не поломка, но
сообщение «Guard запущен» на каждый такой подъём превращает личку с ботом в
ленту перезапусков.

Поэтому запуски считаются, а отчёт уходит только тогда, когда он что-то
значит: первый раз или после долгого перерыва. Частые перезапуски при этом не
замалчиваются — их число попадает в тот же отчёт и в /status.
"""
from __future__ import annotations

import config
import db

KEY_LOG = "start:log"
DAY = 86400
MAX_KEPT = 300           # больше суток всё равно не показываем


async def _recent(now: int) -> list[int]:
    raw = await db.kv_get(KEY_LOG) or ""
    stamps = [int(part) for part in raw.split(",") if part.strip().isdigit()]
    return [at for at in stamps if 0 < now - at < DAY][-MAX_KEPT:]


async def note_start(now: int | None = None) -> dict:
    """Записывает запуск. Возвращает, что о нём известно и стоит ли сообщать."""
    now = now or db.now()
    recent = await _recent(now)
    previous = recent[-1] if recent else 0
    await db.kv_set(KEY_LOG, ",".join(str(at) for at in [*recent, now]))

    gap = now - previous if previous else 0
    return {
        "previous": previous,
        "gap": gap,
        "per_day": len(recent) + 1,
        # Молчим, только если прошлый запуск был совсем недавно: иначе владелец
        # не узнает даже о том, что бот вообще поднялся.
        "quiet": bool(previous and gap < config.STARTUP_QUIET_SEC),
    }


async def starts_per_day(now: int | None = None) -> int:
    return len(await _recent(now or db.now()))


def restless(per_day: int) -> bool:
    """Столько перезапусков за сутки — это уже не норма, а симптом."""
    return per_day >= config.RESTART_ALERT


def note(per_day: int) -> str:
    """Строка для отчёта. Пустая, если перезапусков немного."""
    if not restless(per_day):
        return ""
    return (f"\n\n♻️ **Перезапусков за сутки: {per_day}.**\n"
            f"Так бывает на бесплатном хостинге: сервис засыпает без запросов и "
            f"поднимается заново. Сообщения при этом не теряются — Telegram "
            f"повторяет доставку, — но то, что накопилось после последней копии "
            f"базы, при подъёме не переживает. Лечится HTTP-монитором на "
            f"`/health` раз в 5 минут или тарифом без засыпания.")
