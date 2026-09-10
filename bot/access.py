"""Доступ к боту: заявки и решения администратора.

Администратор — владелец OWNER_ID, тот, кто развернул бота. Остальные попадают
внутрь только после его подтверждения: бот хранит переписку, поэтому пускать
всех подряд нельзя.
"""
from __future__ import annotations

import logging

import config
import db
from bot import parse
from core import fmt, state

log = logging.getLogger("access")

REQUEST_SENT = (
    "🔐 **Доступ пока не открыт.**\n\n"
    "Я отправил владельцу бота заявку — он решит, пускать ли вас. "
    "Как только ответит, я напишу сюда."
)
REQUEST_PENDING = (
    "🔐 Заявка уже отправлена, ждём ответа владельца бота."
)
DENIED = (
    "🚫 Владелец бота отклонил заявку на доступ."
)
APPROVED = (
    "✅ **Доступ открыт!**\n\n"
    "Отправьте /start, чтобы начать."
)
NO_ADMIN = (
    "🔐 Доступ к боту закрыт: владелец не настроил приём заявок.\n"
    "_Администратору: задайте переменную окружения_ `OWNER_ID`."
)


def keyboard(user_id: int) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ Принять", "callback_data": f"ok:{user_id}"},
        {"text": "🚫 Отклонить", "callback_data": f"no:{user_id}"},
    ]]}


def request_card(user: dict, source: str) -> str:
    name = parse.display_name(user)
    username = user.get("username")
    lines = [
        "🔐 **Запрос доступа к боту**",
        f"👤 {name}",
        f"🆔 `{user.get('id')}`",
    ]
    if username:
        lines.append(f"🔗 @{username}")
    lines.append(f"📍 {source}")
    return "\n".join(lines)


async def request(api, user: dict, *, source: str, chat_id: int | None = None) -> str:
    """Регистрирует заявку и уведомляет администратора. Возвращает ответ юзеру."""
    user_id = user.get("id")
    # Смотрим на запись ДО обновления: заявка уже была — значит и уведомляли.
    # Флаг в памяти для этого не годится, он не переживает перезапуск.
    known = await db.get_user(user_id)
    row = await state.remember_user(user_id, name=parse.display_name(user),
                                    chat_id=chat_id)

    if not config.OWNER_ID:
        log.warning("заявка от %s, но OWNER_ID не задан — решать некому", user_id)
        return NO_ADMIN
    if row.get("status") == db.DENIED:
        return DENIED
    if known is not None and known["status"] == db.PENDING:
        return REQUEST_PENDING

    try:
        await api.send_message(state.chat_of(config.OWNER_ID),
                               request_card(user, source),
                               reply_markup=keyboard(user_id))
    except Exception as e:                                   # noqa: BLE001
        log.warning("не удалось отправить заявку администратору: %r", e)
    return REQUEST_SENT


async def decide(api, admin_id: int, action: str, user_id: int) -> tuple[str, str]:
    """Решение по заявке. Возвращает (текст для админа, всплывашка)."""
    if not state.is_admin(admin_id):
        return "", "Решать заявки может только владелец бота."

    approved = action == "ok"
    status = db.APPROVED if approved else db.DENIED
    row = await state.remember_user(user_id, status=status)
    name = row.get("name") or f"id {user_id}"

    try:
        await api.send_message(state.chat_of(user_id),
                               APPROVED if approved else DENIED)
    except Exception as e:                                   # noqa: BLE001
        log.info("не удалось сообщить пользователю %s о решении: %r", user_id, e)

    mark = "✅ Принят" if approved else "🚫 Отклонён"
    card = (f"{mark}\n👤 {name}\n🆔 `{user_id}`\n"
            f"🕒 {fmt.ts(db.now())}")
    return card, ("Доступ открыт" if approved else "Заявка отклонена")


async def revoke(user_id: int) -> bool:
    if not state.is_approved(user_id) or state.is_admin(user_id):
        return False
    await state.remember_user(user_id, status=db.DENIED)
    return True
