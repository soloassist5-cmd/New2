"""Необязательные команды: стикеры, кубики, мелкая анимация.

Прозрачность переживает только стикер: PNG, отправленный фотографией, Telegram
сплющит на белый фон. Поэтому картинки здесь — webp-стикеры, отрисованные из
цветного шрифта эмодзи. Первый показ загружает файл, дальше идёт готовый
file_id, так что повторы мгновенные.
"""
from __future__ import annotations

import asyncio
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
# Lossless на растянутой картинке даёт 60-170 КБ — столько же весит короткое
# видео, и стикер заметно «подгружается» у собеседника. Потерь при 90 глазом не
# видно, а файл вдвое легче и появляется сразу.
WEBP_QUALITY = 90
WEBP_METHOD = 4          # компромисс: сжатие почти как у 6, время — как у 0
KV_PREFIX = "sticker:"
WARMUP_PAUSE = 0.4       # прогрев идёт фоном, спешить некуда — важнее не ловить лимит

# Отрисованные байты в памяти процесса: file_id может не подойти (смена бота,
# чужой чат), и тогда перерисовка не должна стоить ещё одной секунды.
_drawn: dict[str, bytes] = {}


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
        # Исходник — растр 120 px, увеличиваем вчетверо. LANCZOS на таком
        # растяжении звенит по краям: и ореолы видно, и файл вдвое тяжелее.
        big = crop.resize((max(round(crop.width * scale), 1),
                           max(round(crop.height * scale), 1)), Image.BICUBIC)
        buf = io.BytesIO()
        big.save(buf, "WEBP", quality=WEBP_QUALITY, method=WEBP_METHOD)
        return buf.getvalue()
    except Exception as e:                                   # noqa: BLE001
        log.warning("не удалось отрисовать %r: %r", emoji, e)
        return None


async def draw(emoji: str) -> bytes | None:
    """render() в отдельном потоке: Pillow на сложном эмодзи думает секунды."""
    ready = _drawn.get(emoji)
    if ready is not None:
        return ready
    payload = await asyncio.to_thread(render, emoji)
    if payload is not None:            # неудачу не запоминаем: шрифт может появиться
        _drawn[emoji] = payload
    return payload


def forget_drawn() -> None:
    _drawn.clear()


async def send_sticker(api, chat_id: int, emoji: str,
                       connection_id: str | None = None, *,
                       silent: bool = False) -> str | None:
    """Шлёт эмодзи стикером, переиспользуя уже загруженный файл.

    Возвращает file_id — по нему стикер уходит мгновенно, без рисования и
    загрузки. None означает «отправить не вышло».
    """
    cached = await db.kv_get(KV_PREFIX + emoji)
    if cached:
        try:
            await api.send_sticker(chat_id, cached,
                                   business_connection_id=connection_id,
                                   disable_notification=silent)
            return cached
        except Exception as e:                               # noqa: BLE001
            log.info("сохранённый стикер %r не подошёл (%r), рисую заново", emoji, e)

    payload = await draw(emoji)
    if payload is None:
        return None
    try:
        sent = await api.upload_sticker(chat_id, payload, "sticker.webp",
                                        business_connection_id=connection_id,
                                        disable_notification=silent)
    except Exception as e:                                   # noqa: BLE001
        log.warning("стикер не ушёл: %r", e)
        return None

    file_id = ((sent or {}).get("sticker") or {}).get("file_id")
    if file_id:
        await db.kv_set(KV_PREFIX + emoji, file_id)
    return file_id or ""


async def warmup(api, chat_id: int, emojis) -> int:
    """Загружает стикеры заранее, чтобы первый показ ничего не ждал.

    Отправленный один раз файл получает file_id, и дальше стикер уходит
    ссылкой — без Pillow, без загрузки, мгновенно. Отправляем владельцу,
    беззвучно, и сразу убираем: в переписке ничего не остаётся, а file_id
    лежит в базе и переживает передеплой вместе с ней.
    """
    ready, failures = 0, 0
    for emoji in emojis:
        if failures >= 2:
            log.info("прогрев прерван: чат для загрузки недоступен")
            break
        if await db.kv_get(KV_PREFIX + emoji):
            ready += 1
            continue
        payload = await draw(emoji)
        if payload is None:
            continue
        try:
            sent = await api.upload_sticker(chat_id, payload, "sticker.webp",
                                            disable_notification=True)
        except Exception as e:                               # noqa: BLE001
            log.info("прогрев %r не удался: %r", emoji, e)
            failures += 1
            continue
        failures = 0

        file_id = ((sent or {}).get("sticker") or {}).get("file_id")
        if file_id:
            await db.kv_set(KV_PREFIX + emoji, file_id)
            ready += 1
        message_id = (sent or {}).get("message_id")
        if message_id:
            try:
                await api.delete_message(chat_id, message_id)
            except Exception as e:                           # noqa: BLE001
                log.debug("прогревочный стикер остался в чате: %r", e)
        await asyncio.sleep(WARMUP_PAUSE)
    return ready


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
