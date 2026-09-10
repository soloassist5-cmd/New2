"""Разбор сообщений Bot API."""
from __future__ import annotations

# Порядок важен: у гифки в апдейте есть и animation, и document;
# у кружка — video_note, а не video.
MEDIA_FIELDS = (
    ("photo", "фото"),
    ("animation", "гифка"),
    ("video_note", "видеосообщение"),
    ("video", "видео"),
    ("voice", "голосовое"),
    ("audio", "аудио"),
    ("sticker", "стикер"),
    ("document", "документ"),
)

SERVICE_FIELDS = ("new_chat_members", "left_chat_member", "pinned_message",
                  "new_chat_title", "new_chat_photo", "group_chat_created")


def media_of(message: dict) -> tuple[str | None, str | None]:
    """Возвращает (тип медиа, file_id). file_id остаётся валидным после удаления."""
    for field, kind in MEDIA_FIELDS:
        value = message.get(field)
        if not value:
            continue
        if field == "photo":
            return kind, value[-1]["file_id"]      # последний размер — самый крупный
        return kind, value.get("file_id")
    return None, None


def text_of(message: dict) -> str:
    return message.get("text") or message.get("caption") or ""


def display_name(user: dict | None) -> str:
    if not user:
        return "неизвестно"
    name = " ".join(filter(None, (user.get("first_name"), user.get("last_name"))))
    if name:
        return name
    username = user.get("username")
    return f"@{username}" if username else f"id {user.get('id')}"


def chat_title(chat: dict | None) -> str:
    if not chat:
        return "неизвестно"
    return chat.get("title") or display_name(chat)


def is_service(message: dict) -> bool:
    return any(field in message for field in SERVICE_FIELDS)
