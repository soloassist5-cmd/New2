"""Необязательные команды: стикеры, кубики, мелкая анимация.

Прозрачность переживает только стикер: PNG, отправленный фотографией, Telegram
сплющит на белый фон. Поэтому картинки здесь — webp-стикеры, отрисованные из
цветного шрифта эмодзи. Первый показ загружает файл, дальше идёт готовый
file_id, так что повторы мгновенные.
"""
from __future__ import annotations

import io
import logging
import random
import unicodedata

import db

log = logging.getLogger("fun")

FONT_PATHS = (
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/NotoColorEmoji.ttf",
)
# У цветного Noto единственный растровый размер — другие Pillow не принимает.
FONT_SIZE = 109
CANVAS = 136
STICKER_SIDE = 512
KV_PREFIX = "sticker:"


def font_path() -> str | None:
    from pathlib import Path

    for path in FONT_PATHS:
        if Path(path).exists():
            return path
    return None


def render(emoji: str) -> bytes | None:
    """Эмодзи → прозрачный webp 512px. None, если рисовать нечем."""
    path = font_path()
    if path is None:
        log.warning("шрифт цветных эмодзи не найден — стикеры недоступны")
        return None
    try:
        from PIL import Image, ImageDraw, ImageFont

        font = ImageFont.truetype(path, FONT_SIZE)
        canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
        ImageDraw.Draw(canvas).text((CANVAS // 2, CANVAS // 2), emoji, font=font,
                                    embedded_color=True, anchor="mm")
        box = canvas.getbbox()
        if box is None:
            return None                     # шрифт не знает такого символа
        crop = canvas.crop(box)
        scale = STICKER_SIDE / max(crop.size)
        big = crop.resize((max(round(crop.width * scale), 1),
                           max(round(crop.height * scale), 1)), Image.LANCZOS)
        buf = io.BytesIO()
        big.save(buf, "WEBP", lossless=True)
        return buf.getvalue()
    except Exception as e:                                   # noqa: BLE001
        log.warning("не удалось отрисовать %r: %r", emoji, e)
        return None


async def send_sticker(api, chat_id: int, emoji: str,
                       connection_id: str | None = None) -> bool:
    """Шлёт эмодзи стикером, переиспользуя уже загруженный файл."""
    cached = await db.kv_get(KV_PREFIX + emoji)
    if cached:
        try:
            await api.send_sticker(chat_id, cached,
                                   business_connection_id=connection_id)
            return True
        except Exception as e:                               # noqa: BLE001
            log.info("сохранённый стикер %r не подошёл (%r), рисую заново", emoji, e)

    payload = render(emoji)
    if payload is None:
        return False
    try:
        sent = await api.upload_sticker(chat_id, payload, "sticker.webp",
                                        business_connection_id=connection_id)
    except Exception as e:                                   # noqa: BLE001
        log.warning("стикер не ушёл: %r", e)
        return False

    file_id = ((sent or {}).get("sticker") or {}).get("file_id")
    if file_id:
        await db.kv_set(KV_PREFIX + emoji, file_id)
    return True


def is_emoji(text: str) -> bool:
    """Один символ-картинка, а не кусок текста."""
    stripped = text.strip()
    if not stripped or len(stripped) > 8:
        return False
    return any(unicodedata.category(ch) == "So" or ord(ch) > 0x1F000
               for ch in stripped)


# ------------------------------------------------------------ случайности ---

EIGHT_BALL = (
    "Бесспорно", "Мне кажется — да", "Пока неясно, попробуй снова",
    "Даже не думай", "Определённо да", "Не рассчитывай на это",
    "Знаки говорят — да", "Спроси позже", "Весьма сомнительно",
    "Можешь быть уверен в этом", "Мой ответ — нет", "Скорее всего",
    "Сконцентрируйся и спроси опять", "Никаких сомнений", "Перспективы не очень",
)

COIN = ("Орёл", "Решка")


def roll(spec: str) -> tuple[list[int], int, int] | None:
    """«2d6» → (броски, сумма, граней). None, если запись непонятна."""
    spec = (spec or "1d6").lower().replace(" ", "")
    count, _, sides = spec.partition("d")
    try:
        count = int(count or 1)
        sides = int(sides or 6)
    except ValueError:
        return None
    if not (1 <= count <= 20 and 2 <= sides <= 1000):
        return None
    throws = [random.randint(1, sides) for _ in range(count)]
    return throws, sum(throws), sides


def eight_ball() -> str:
    return random.choice(EIGHT_BALL)


def coin() -> str:
    return random.choice(COIN)


def choose(raw: str) -> str | None:
    options = [part.strip() for part in raw.replace("|", ",").split(",")
               if part.strip()]
    return random.choice(options) if len(options) >= 2 else None
