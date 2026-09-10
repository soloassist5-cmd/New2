"""Восстановление связки Business после перезапуска с пустой базой."""
import asyncio

import pytest

import config
import db
from bot import business
from core import state
from tests.fakes import ALL_RIGHTS, FakeBotAPI, approve, business_message

OWNER = 111
PEER = 777
BIZ = "biz1"

CONNECTION = {
    "id": BIZ,
    "user": {"id": OWNER, "first_name": "Владелец"},
    "user_chat_id": OWNER,
    "is_enabled": True,
    "rights": dict(ALL_RIGHTS),
}


@pytest.fixture(autouse=True)
def env():
    from core.chatprefs import invalidate_all

    config.DB_PATH.unlink(missing_ok=True)
    asyncio.run(db.init())
    invalidate_all()
    config.PURGE_DEBOUNCE_SEC = 0
    business._pending.clear()
    business._relearn_attempt.clear()
    business._relearn_announced.clear()
    state.business.clear()
    state.mutes.clear()
    state.forget_own_deletions()
    state.users.clear()
    state.allowlist.clear()
    config.OWNER_ID = OWNER        # владелец бота — он же администратор
    approve(OWNER, OWNER)
    state.client = None
    state.log_entity = None
    state.api = FakeBotAPI()
    state.api.connection = dict(CONNECTION)
    yield
    config.OWNER_ID = 0
    asyncio.run(db.close())
    state.api = None
    state.business.clear()


def incoming(**kwargs):
    asyncio.run(business.on_business_message(state.api, business_message(**kwargs)))


def test_first_message_restores_a_forgotten_connection():
    """База стёрлась при передеплое — Telegram business_connection заново не шлёт."""
    assert state.business == {}
    incoming(text="привет", message_id=1)

    assert state.api.connection_lookups == 1
    assert state.business[BIZ]["user_id"] == OWNER
    assert asyncio.run(db.all_business()), "связка сохранена в базу"


def test_restored_connection_keeps_working():
    incoming(text="привет", message_id=1)
    state.api.sent.clear()
    asyncio.run(state.mute_user(OWNER, PEER, PEER, 0))
    incoming(text="спам", message_id=2)
    assert state.api.deleted == [(BIZ, [2])], "мут работает сразу после восстановления"


def test_owner_is_told_once():
    incoming(text="привет", message_id=1)
    notes = [t for _, t, _ in state.api.sent if "восстановлено" in t]
    assert len(notes) == 1

    state.business.clear()
    business._relearn_attempt.clear()
    incoming(text="ещё", message_id=2)
    notes = [t for _, t, _ in state.api.sent if "восстановлено" in t]
    assert len(notes) == 1, "повторно не сообщаем"


def test_known_connection_is_not_re_fetched():
    incoming(text="привет", message_id=1)
    incoming(text="ещё", message_id=2)
    incoming(text="и ещё", message_id=3)
    assert state.api.connection_lookups == 1


def test_failed_lookup_is_not_hammered():
    state.api.connection = None            # Telegram отвечает ошибкой
    incoming(text="привет", message_id=1)
    incoming(text="ещё", message_id=2)
    assert state.api.connection_lookups == 1, "повтор не чаще раза в минуту"


def test_unknown_owner_never_deletes_anything():
    """Без владельца нельзя отличить свои сообщения от чужих — удалять опасно."""
    state.api.connection = None
    asyncio.run(state.set_dnd(OWNER))
    incoming(text="важное", message_id=1)

    assert state.api.deleted == [], "вслепую ничего не удаляем"
    assert asyncio.run(db.get_message(0, PEER, 1)) is None, "владелец неизвестен"


def test_deletion_event_also_restores_the_connection():
    asyncio.run(business.on_deleted_business_messages(state.api, {
        "business_connection_id": BIZ,
        "chat": {"id": PEER, "type": "private", "first_name": "Вася"},
        "message_ids": [5],
    }))
    assert state.business[BIZ]["user_id"] == OWNER
