"""Форматирование текста, времени и упоминаний."""
from __future__ import annotations

import datetime as _dt
import re

from telethon import utils

TIME_RE = re.compile(r"^(\d+)\s*(s|sec|с|сек|m|min|м|мин|h|hour|ч|час|d|day|д|дн|w|нед)?$",
                     re.IGNORECASE)

_UNITS = {
    "s": 1, "sec": 1, "с": 1, "сек": 1,
    "m": 60, "min": 60, "м": 60, "мин": 60,
    "h": 3600, "hour": 3600, "ч": 3600, "час": 3600,
    "d": 86400, "day": 86400, "д": 86400, "дн": 86400,
    "w": 604800, "нед": 604800,
}


def parse_duration(raw: str) -> int | None:
    """'10m' -> 600. Возвращает None, если строка не похожа на срок."""
    if not raw:
        return None
    m = TIME_RE.match(raw.strip())
    if not m:
        return None
    value, unit = m.group(1), (m.group(2) or "m").lower()
    return int(value) * _UNITS.get(unit, 60)


_SCALE = (
    (604800, ("неделю", "недели", "недель")),
    (86400, ("день", "дня", "дней")),
    (3600, ("час", "часа", "часов")),
    (60, ("минуту", "минуты", "минут")),
)


def plural(n: int, forms: tuple[str, str, str]) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return forms[2]
    n %= 10
    if n == 1:
        return forms[0]
    if 2 <= n <= 4:
        return forms[1]
    return forms[2]


def human_delta(seconds: int) -> str:
    """600 -> '10 минут', 3660 -> '1 час 1 минуту'."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} {plural(seconds, ('секунду', 'секунды', 'секунд'))}"

    # Одна единица — если срок кратен ей и не дотягивает до следующей по старшинству.
    for i, (div, forms) in enumerate(_SCALE):
        bigger = _SCALE[i - 1][0] if i else None
        if seconds >= div and seconds % div == 0 and (bigger is None or seconds < bigger):
            n = seconds // div
            return f"{n} {plural(n, forms)}"

    parts = []
    for div, forms in _SCALE[1:]:          # недели в составном виде не нужны
        if seconds >= div:
            n, seconds = divmod(seconds, div)
            parts.append(f"{n} {plural(n, forms)}")
    return " ".join(parts) or "меньше минуты"


def uptime(seconds: float) -> str:
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}д {h}ч {m}м"
    if h:
        return f"{h}ч {m}м {s}с"
    return f"{m}м {s}с"


def ts(unix: int | None) -> str:
    if not unix:
        return "—"
    return _dt.datetime.fromtimestamp(unix).strftime("%d.%m.%Y %H:%M:%S")


def size(num: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if num < 1024 or unit == "ГБ":
            return f"{num:.0f} {unit}" if unit == "Б" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} ГБ"


def name_of(entity) -> str:
    if entity is None:
        return "неизвестно"
    try:
        return utils.get_display_name(entity) or "без имени"
    except Exception:
        return "без имени"


def mention(entity, user_id: int | None = None) -> str:
    """Кликабельное упоминание в markdown."""
    uid = user_id if user_id is not None else getattr(entity, "id", None)
    return f"[{name_of(entity)}](tg://user?id={uid})" if uid else name_of(entity)


def truncate(text: str | None, limit: int = 3000) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def code(text: str) -> str:
    return f"`{text}`"
