"""Срочный вызов: способ достучаться сквозь «Не беспокоить»."""
import asyncio

import pytest

import config
import db
from bot import business, commands, dotcmd, urgent
from core import chatprefs, state
from tests.fakes import ALL_RIGHTS, FakeBotAPI, approve, business_message

OWNER, PEER, OTHER = 111, 777, 888
BIZ = "biz1"


@pytest.fixture(autouse=True)
def env():
    config.DB_PATH.unlink(missing_ok=True)
    asyncio.run(db.init())
    chatprefs.invalidate_all()
    config.OWNER_ID = OWNER
    config.PURGE_DEBOUNCE_SEC = 0
    config.URGENT_ENABLED = True
    config.URGENT_COOLDOWN_HOURS = 24
    state.users.clear()
    state.business.clear()
    state.mutes.clear()
    state.allowlist.clear()
    state.forget_own_deletions()
    state.dnd_notified.clear()
    urgent.forget_refusals()
    approve(OWNER, OWNER)
    state.business[BIZ] = {"user_id": OWNER, "user_chat_id": OWNER,
                           "is_enabled": True, "rights": dict(ALL_RIGHTS)}
    state.api = FakeBotAPI()
    asyncio.run(state.set_dnd(OWNER))
    state.api.sent.clear()
    yield
    config.OWNER_ID = 0
    asyncio.run(db.close())
    state.api = None
    state.users.clear()
    state.business.clear()


def writes(text, *, from_id=PEER, msg_id=1, name="Вася"):
    asyncio.run(business.on_business_message(state.api, business_message(
        text, connection_id=BIZ, chat_id=from_id, from_id=from_id,
        message_id=msg_id, first_name=name)))
    return state.api


def to_sender(api, chat_id=PEER) -> list[str]:
    return [text for cid, text, biz in api.sent if cid == chat_id and biz]


# ---------------------------------------------------------------- разбор ---

@pytest.mark.parametrize("text,reason", [
    ("/срочно", ""),
    ("!срочно", ""),
    ("/срочно перезвони", "перезвони"),
    ("/СРОЧНО кричу", "кричу"),
    ("/urgent call me", "call me"),
    ("!sos", ""),
    ("  /срочно, мама в больнице", "мама в больнице"),
])
def test_recognises_a_call(text, reason):
    assert urgent.match(text) == reason


@pytest.mark.parametrize("text", [
    "это срочно вообще-то",
    "срочно перезвони",          # без / и ! — обычная речь
    "/срочность работы",         # другое слово
    "привет",
    "",
])
def test_does_not_mistake_ordinary_speech(text):
    assert urgent.match(text) is None


def test_disabled_feature_recognises_nothing():
    config.URGENT_ENABLED = False
    try:
        assert urgent.match("/срочно") is None
        assert urgent.hint() == ""
    finally:
        config.URGENT_ENABLED = True


# ----------------------------------------------------------------- вызов ---

def test_call_reaches_the_owner_with_the_reason():
    api = writes("/срочно мама в больнице")
    card = [t for t in api.texts_to(OWNER) if "Срочный вызов" in t]
    assert len(card) == 1
    assert "мама в больнице" in card[0] and str(PEER) in card[0]
    assert "Вася" in card[0]


def test_sender_gets_a_confirmation():
    api = writes("/срочно перезвони")
    assert any("Принято" in text for text in to_sender(api))


def test_call_is_saved_for_later():
    writes("/срочно перезвони")
    rows = asyncio.run(db.urgent_calls(OWNER))
    assert len(rows) == 1 and rows[0]["reason"] == "перезвони"
    assert rows[0]["user_id"] == PEER


def test_the_message_itself_is_still_removed():
    """Режим есть режим: сообщение уходит из переписки, но не теряется."""
    api = writes("/срочно перезвони", msg_id=7)
    assert api.deleted == [(BIZ, [7])]
    saved = asyncio.run(db.intercepted(OWNER, PEER))
    assert saved and saved[0]["reason"] == "urgent"


def test_call_skips_the_ordinary_auto_reply():
    api = writes("/срочно перезвони")
    assert not any("не доходят" in text for text in to_sender(api))
    assert not any("Не беспокоить» работает" in t for t in api.texts_to(OWNER))


# ----------------------------------------------------------------- квота ---

def test_second_call_the_same_day_is_refused():
    writes("/срочно раз", msg_id=1)
    state.api.sent.clear()
    api = writes("/срочно два", msg_id=2)

    assert not any("Срочный вызов" in t for t in api.texts_to(OWNER)), "второго нет"
    assert any("уже отправлен" in text for text in to_sender(api))
    assert len(asyncio.run(db.urgent_calls(OWNER))) == 1


def test_refusals_do_not_turn_into_a_flood():
    """Отказ шлётся от имени владельца — на каждую попытку отвечать нельзя."""
    writes("/срочно раз", msg_id=1)
    state.api.sent.clear()
    for msg_id in range(2, 8):
        writes("/срочно ещё", msg_id=msg_id)
    assert len(to_sender(state.api)) == 1


def test_the_quota_is_per_person():
    writes("/срочно раз", from_id=PEER, msg_id=1)
    state.api.sent.clear()
    api = writes("/срочно и мне", from_id=OTHER, msg_id=2, name="Петя")
    assert any("Срочный вызов" in t for t in api.texts_to(OWNER))
    assert len(asyncio.run(db.urgent_calls(OWNER))) == 2


def test_the_quota_expires():
    writes("/срочно вчера", msg_id=1)
    asyncio.run(db.execute("UPDATE urgent_calls SET at=?",
                           (db.now() - 25 * 3600,)))
    urgent.forget_refusals()
    state.api.sent.clear()

    api = writes("/срочно сегодня", msg_id=2)
    assert any("Срочный вызов" in t for t in api.texts_to(OWNER))
    assert len(asyncio.run(db.urgent_calls(OWNER))) == 2


def test_the_quota_survives_a_restart():
    """Квота в памяти обнулялась бы при каждом обновлении бота."""
    writes("/срочно раз", msg_id=1)
    urgent.forget_refusals()
    state.api.sent.clear()

    api = writes("/срочно снова", msg_id=2)
    assert not any("Срочный вызов" in t for t in api.texts_to(OWNER))


# ------------------------------------------------------- вне режима ------

def test_outside_do_not_disturb_it_is_an_ordinary_message():
    asyncio.run(state.clear_dnd(OWNER))
    state.api.sent.clear()
    api = writes("/срочно перезвони", msg_id=9)

    assert api.deleted == [], "ничего не удаляем"
    assert asyncio.run(db.urgent_calls(OWNER)) == []
    assert asyncio.run(db.get_message(OWNER, PEER, 9)) is not None


def test_whitelisted_people_are_not_affected():
    asyncio.run(state.allow_user(OWNER, PEER, "Вася"))
    state.api.sent.clear()
    writes("/срочно перезвони", msg_id=10)
    assert asyncio.run(db.urgent_calls(OWNER)) == [], "их сообщения и так доходят"


# --------------------------------------------------------- для владельца --

def test_owner_sees_the_call_history():
    writes("/срочно мама в больнице")
    state.api.sent.clear()
    asyncio.run(commands.handle(state.api, {
        "message_id": 1, "date": 1700000000,
        "chat": {"id": OWNER, "type": "private"},
        "from": {"id": OWNER, "first_name": "Я"}, "text": "/urgent"}))
    listing = state.api.texts_to(OWNER)[-1]
    assert "мама в больнице" in listing and "Вася" in listing


def test_history_is_available_from_a_chat_too():
    writes("/срочно перезвони")
    state.api.sent.clear()
    asyncio.run(dotcmd.handle(state.api, business_message(
        ".urgent", connection_id=BIZ, chat_id=PEER, from_id=OWNER,
        message_id=20), BIZ, OWNER))
    assert "перезвони" in state.api.texts_to(OWNER)[-1]


def test_empty_history_explains_the_feature():
    state.api.sent.clear()
    asyncio.run(commands.handle(state.api, {
        "message_id": 1, "date": 1700000000,
        "chat": {"id": OWNER, "type": "private"},
        "from": {"id": OWNER, "first_name": "Я"}, "text": "/urgent"}))
    assert "по одному разу в сутки" in state.api.texts_to(OWNER)[-1]


def test_auto_reply_tells_people_about_the_call():
    api = writes("привет")
    reply = to_sender(api)[0]
    assert "/срочно" in reply and "раз в сутки" in reply
