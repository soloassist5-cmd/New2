"""Доступ к боту: заявки, кнопки администратора, отзыв доступа."""
import asyncio

import pytest

import config
import db
from bot import business, commands
from core import chatprefs, state
from tests.fakes import ALL_RIGHTS, FakeBotAPI, approve, business_message

ADMIN = 111
STRANGER = 555
BIZ = "biz-stranger"


def message(text, *, from_id, name="Незнакомец"):
    return {"message_id": 1, "date": 1700000000,
            "chat": {"id": from_id, "type": "private"},
            "from": {"id": from_id, "first_name": name, "username": "who"},
            "text": text}


def callback(data, *, from_id=ADMIN, card_chat=ADMIN, card_id=7):
    return {"id": "cb1", "data": data, "from": {"id": from_id},
            "message": {"message_id": card_id, "chat": {"id": card_chat}}}


@pytest.fixture(autouse=True)
def env():
    config.DB_PATH.unlink(missing_ok=True)
    asyncio.run(db.init())
    chatprefs.invalidate_all()
    config.PURGE_DEBOUNCE_SEC = 0
    config.OWNER_ID = ADMIN
    state.users.clear()
    state.business.clear()
    state.mutes.clear()
    state.allowlist.clear()
    state.forget_own_deletions()
    business._relearn_announced.clear()
    approve(ADMIN, ADMIN)
    state.bot_user = {"username": "guard_bot"}
    state.api = FakeBotAPI()
    yield
    config.OWNER_ID = 0
    asyncio.run(db.close())
    state.api = None
    state.users.clear()


def run(text, **kwargs):
    asyncio.run(commands.handle(state.api, message(text, **kwargs)))
    return state.api


def press(data, **kwargs):
    asyncio.run(commands.handle_callback(state.api, callback(data, **kwargs)))
    return state.api


# ------------------------------------------------------------- заявка ------

def test_stranger_start_creates_a_request():
    api = run("/start", from_id=STRANGER)

    to_user = api.texts_to(STRANGER)
    assert any("Доступ пока не открыт" in text for text in to_user)

    to_admin = api.texts_to(ADMIN)
    assert any("Запрос доступа" in text for text in to_admin)
    assert any(str(STRANGER) in text for text in to_admin), "id виден администратору"
    assert any("@who" in text for text in to_admin)


def test_request_card_carries_two_buttons():
    api = run("/start", from_id=STRANGER)
    markups = [m for m in api.markups if m]
    assert markups, "кнопки приложены"
    buttons = markups[-1]["inline_keyboard"][0]
    assert [b["text"] for b in buttons] == ["✅ Принять", "🚫 Отклонить"]
    assert buttons[0]["callback_data"] == f"ok:{STRANGER}"
    assert buttons[1]["callback_data"] == f"no:{STRANGER}"


def test_pending_request_is_not_duplicated():
    run("/start", from_id=STRANGER)
    state.api.sent.clear()
    api = run("/start", from_id=STRANGER)
    assert not any("Запрос доступа" in text for text in api.texts_to(ADMIN))
    assert any("уже отправлена" in text for text in api.texts_to(STRANGER))


def test_data_commands_also_ask_for_access():
    api = run("/deleted", from_id=STRANGER)
    assert any("Запрос доступа" in text for text in api.texts_to(ADMIN))
    assert not any("Последние удалённые" in text for text in api.texts)


def test_admin_needs_no_approval():
    api = run("/status", from_id=ADMIN)
    assert any("Состояние" in text for text in api.texts_to(ADMIN))


# ------------------------------------------------------------ решение ------

def test_approve_opens_access():
    run("/start", from_id=STRANGER)
    state.api.sent.clear()
    api = press(f"ok:{STRANGER}")

    assert state.is_approved(STRANGER)
    assert any("Доступ открыт" in text for text in api.texts_to(STRANGER))
    assert api.callbacks[-1][1] == "Доступ открыт"
    assert "Принят" in api.edits[-1][2], "карточка заявки обновлена"

    state.api.sent.clear()
    api = run("/status", from_id=STRANGER)
    assert any("Состояние" in text for text in api.texts_to(STRANGER))


def test_deny_closes_access():
    run("/start", from_id=STRANGER)
    state.api.sent.clear()
    api = press(f"no:{STRANGER}")

    assert not state.is_approved(STRANGER)
    assert any("отклонил" in text for text in api.texts_to(STRANGER))
    assert "Отклонён" in api.edits[-1][2]


def test_denied_user_is_not_asked_again():
    run("/start", from_id=STRANGER)
    press(f"no:{STRANGER}")
    state.api.sent.clear()

    api = run("/start", from_id=STRANGER)
    assert not any("Запрос доступа" in text for text in api.texts_to(ADMIN))
    assert any("отклонил" in text for text in api.texts_to(STRANGER))


def test_only_admin_may_decide():
    run("/start", from_id=STRANGER)
    state.api.sent.clear()
    api = press(f"ok:{STRANGER}", from_id=STRANGER)     # сам себя одобрить нельзя

    assert not state.is_approved(STRANGER)
    assert "только владелец" in api.callbacks[-1][1].lower()
    assert api.edits == []


def test_broken_callback_is_answered_and_ignored():
    api = press("мусор")
    assert api.callbacks, "всплывашку всё равно закрываем"
    assert api.edits == []


# --------------------------------------------------------- управление ------

def test_users_lists_everyone():
    run("/start", from_id=STRANGER)
    press(f"ok:{STRANGER}")
    state.api.sent.clear()

    api = run("/users", from_id=ADMIN)
    listing = api.texts_to(ADMIN)[0]
    assert str(STRANGER) in listing and "доступ открыт" in listing


def test_users_is_admin_only():
    run("/start", from_id=STRANGER)
    press(f"ok:{STRANGER}")
    state.api.sent.clear()

    api = run("/users", from_id=STRANGER)
    assert api.sent == [], "обычному пользователю список не положен"


def test_revoke_closes_access():
    run("/start", from_id=STRANGER)
    press(f"ok:{STRANGER}")
    state.api.sent.clear()

    api = run(f"/revoke {STRANGER}", from_id=ADMIN)
    assert not state.is_approved(STRANGER)
    assert "закрыт" in api.texts_to(ADMIN)[0]


def test_admin_cannot_revoke_himself():
    api = run(f"/revoke {ADMIN}", from_id=ADMIN)
    assert state.is_approved(ADMIN)
    assert "не было доступа" in api.texts_to(ADMIN)[0]


# ------------------------------------------------- подключение Business ----

def connection(user_id):
    return {"id": BIZ, "user": {"id": user_id, "first_name": "Незнакомец"},
            "user_chat_id": user_id, "is_enabled": True,
            "rights": dict(ALL_RIGHTS)}


def test_unapproved_connection_asks_for_access():
    asyncio.run(business.on_business_connection(state.api, connection(STRANGER)))
    api = state.api
    assert any("Запрос доступа" in text for text in api.texts_to(ADMIN))
    assert any("Доступ пока не открыт" in text for text in api.texts_to(STRANGER))
    assert not any("подключён" in text for text in api.texts_to(STRANGER))


def test_unapproved_chats_are_not_served():
    """Пока доступ не открыт, переписку не трогаем и не сохраняем."""
    asyncio.run(business.on_business_connection(state.api, connection(STRANGER)))
    asyncio.run(business.on_business_message(
        state.api, business_message("привет", connection_id=BIZ, chat_id=777,
                                    from_id=777)))
    assert asyncio.run(db.get_message(STRANGER, 777, 10)) is None


def test_approved_connection_gets_the_welcome():
    asyncio.run(business.on_business_connection(state.api, connection(STRANGER)))
    press(f"ok:{STRANGER}")
    state.api.sent.clear()
    asyncio.run(business.on_business_connection(state.api, connection(STRANGER)))
    assert any("подключён" in text for text in state.api.texts_to(STRANGER))


def test_no_admin_configured_is_explained():
    config.OWNER_ID = 0
    api = run("/start", from_id=STRANGER)
    assert any("OWNER_ID" in text for text in api.texts_to(STRANGER))


def test_pending_request_survives_a_restart():
    """Флаг «уже уведомили» в памяти не пережил бы перезапуск и спамил бы админа."""
    run("/start", from_id=STRANGER)
    state.users.clear()                     # как после передеплоя
    asyncio.run(state.load_users())
    state.api.sent.clear()

    api = run("/start", from_id=STRANGER)
    assert not any("Запрос доступа" in text for text in api.texts_to(ADMIN))
