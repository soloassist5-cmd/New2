"""Изоляция данных между владельцами.

Бот раздаётся нескольким людям, а id личного чата в Bot API совпадает с id
собеседника и одинаков для всех владельцев. Без owner_id в ключе пользователи
видели бы переписку друг друга — это самое опасное, что здесь может сломаться.
"""
import asyncio

import pytest

import config
import db
from bot import business, commands, dotcmd
from core import chatprefs, state
from tests.fakes import ALL_RIGHTS, FakeBotAPI, approve, business_message

ANNA, BORIS = 111, 222
BIZ_ANNA, BIZ_BORIS = "biz-anna", "biz-boris"
PEER = 777                    # один и тот же собеседник у обоих


@pytest.fixture(autouse=True)
def env():
    config.DB_PATH.unlink(missing_ok=True)
    asyncio.run(db.init())
    chatprefs.invalidate_all()
    config.PURGE_DEBOUNCE_SEC = 0
    config.ANIM_DELAY = 0
    config.OWNER_ID = ANNA
    state.users.clear()
    state.business.clear()
    state.mutes.clear()
    state.allowlist.clear()
    state.forget_own_deletions()
    business._pending.clear()
    approve(ANNA, ANNA, name="Анна")
    approve(BORIS, BORIS, name="Борис")
    state.business[BIZ_ANNA] = {"user_id": ANNA, "user_chat_id": ANNA,
                                "is_enabled": True, "rights": dict(ALL_RIGHTS)}
    state.business[BIZ_BORIS] = {"user_id": BORIS, "user_chat_id": BORIS,
                                 "is_enabled": True, "rights": dict(ALL_RIGHTS)}
    state.api = FakeBotAPI()
    yield
    config.OWNER_ID = 0
    asyncio.run(db.close())
    state.api = None
    state.users.clear()
    state.business.clear()


def incoming(connection_id, text, msg_id):
    asyncio.run(business.on_business_message(
        state.api, business_message(text, connection_id=connection_id,
                                    chat_id=PEER, from_id=PEER, message_id=msg_id)))


def deleted(connection_id, ids):
    asyncio.run(business.on_deleted_business_messages(state.api, {
        "business_connection_id": connection_id,
        "chat": {"id": PEER, "type": "private", "first_name": "Общий"},
        "message_ids": list(ids)}))


def command(text, owner_id, connection_id, msg_id=99):
    asyncio.run(dotcmd.handle(
        state.api,
        business_message(text, connection_id=connection_id, chat_id=PEER,
                         from_id=owner_id, message_id=msg_id),
        connection_id, owner_id))


def bot_command(text, user_id):
    asyncio.run(commands.handle(state.api, {
        "message_id": 1, "date": 1700000000,
        "chat": {"id": user_id, "type": "private"},
        "from": {"id": user_id, "first_name": "Кто-то"}, "text": text}))


# ------------------------------------------------------------ переписка ----

def test_same_peer_message_ids_do_not_collide():
    """У обоих владельцев чат с PEER и сообщение №5 — это разные сообщения."""
    incoming(BIZ_ANNA, "секрет Анны", 5)
    incoming(BIZ_BORIS, "секрет Бориса", 5)

    assert asyncio.run(db.get_message(ANNA, PEER, 5))["text"] == "секрет Анны"
    assert asyncio.run(db.get_message(BORIS, PEER, 5))["text"] == "секрет Бориса"


def test_deletion_report_goes_only_to_its_owner():
    incoming(BIZ_ANNA, "секрет Анны", 5)
    incoming(BIZ_BORIS, "секрет Бориса", 5)
    state.api.sent.clear()

    deleted(BIZ_ANNA, [5])

    assert any("секрет Анны" in text for text in state.api.texts_to(ANNA))
    assert state.api.texts_to(BORIS) == [], "Борису чужие удаления не показываем"
    assert asyncio.run(db.get_message(BORIS, PEER, 5)) is not None, "его кэш цел"


def test_journal_is_separate():
    incoming(BIZ_ANNA, "секрет Анны", 5)
    incoming(BIZ_BORIS, "секрет Бориса", 6)
    deleted(BIZ_ANNA, [5])
    deleted(BIZ_BORIS, [6])
    state.api.sent.clear()

    bot_command("/deleted", ANNA)
    listing = state.api.texts_to(ANNA)[0]
    assert "секрет Анны" in listing and "секрет Бориса" not in listing


def test_search_does_not_cross_owners():
    incoming(BIZ_ANNA, "пароль от сейфа", 5)
    incoming(BIZ_BORIS, "пароль от сейфа", 6)
    deleted(BIZ_ANNA, [5])
    deleted(BIZ_BORIS, [6])
    state.api.sent.clear()

    bot_command("/find пароль", BORIS)
    found = state.api.texts_to(BORIS)[0]
    assert "Найдено: 1" in found


# ---------------------------------------------------------------- муты -----

def test_mute_applies_only_to_its_owner():
    command(".mute", ANNA, BIZ_ANNA)
    state.api.deleted.clear()

    incoming(BIZ_BORIS, "привет Борису", 7)
    assert state.api.deleted == [], "мут Анны не глушит чужие чаты"

    incoming(BIZ_ANNA, "привет Анне", 8)
    assert state.api.deleted == [(BIZ_ANNA, [8])]


def test_global_mute_stays_inside_one_owner():
    command(".mute -all", ANNA, BIZ_ANNA)
    state.api.deleted.clear()
    incoming(BIZ_BORIS, "привет", 9)
    assert state.api.deleted == []


def test_own_deletion_marks_do_not_cross_owners():
    incoming(BIZ_BORIS, "важное", 11)
    state.mark_own_deletion(ANNA, PEER, 11)      # Анна удалила своё сообщение №11
    state.api.sent.clear()

    deleted(BIZ_BORIS, [11])
    assert any("важное" in text for text in state.api.texts_to(BORIS)), \
        "отчёт Бориса не должен глохнуть из-за действий Анны"


# -------------------------------------------------------- не беспокоить ----

def test_dnd_is_personal():
    bot_command("/gmute", ANNA)
    assert state.dnd_active(ANNA) and not state.dnd_active(BORIS)

    state.api.deleted.clear()
    incoming(BIZ_BORIS, "привет Борису", 12)
    assert state.api.deleted == [], "у Бориса режим не включён"

    incoming(BIZ_ANNA, "привет Анне", 13)
    assert state.api.deleted == [(BIZ_ANNA, [13])]


def test_allowlist_is_personal():
    asyncio.run(state.allow_user(ANNA, PEER, "свой"))
    bot_command("/gmute", ANNA)
    bot_command("/gmute", BORIS)
    state.api.deleted.clear()

    incoming(BIZ_ANNA, "пропусти", 14)
    assert state.api.deleted == [], "у Анны собеседник в белом списке"

    incoming(BIZ_BORIS, "пропусти", 15)
    assert state.api.deleted == [(BIZ_BORIS, [15])], "у Бориса — нет"


# ------------------------------------------------------------ настройки ----

def test_chat_settings_are_personal():
    asyncio.run(db.set_setting(ANNA, PEER, "ignored", 1))
    chatprefs.invalidate_all()

    incoming(BIZ_ANNA, "не сохраняй", 16)
    incoming(BIZ_BORIS, "сохрани", 17)

    assert asyncio.run(db.get_message(ANNA, PEER, 16)) is None
    assert asyncio.run(db.get_message(BORIS, PEER, 17)) is not None


def test_stats_count_only_your_own():
    incoming(BIZ_ANNA, "раз", 18)
    incoming(BIZ_ANNA, "два", 19)
    incoming(BIZ_BORIS, "три", 20)

    assert (asyncio.run(db.stats(ANNA)))["cached"] == 2
    assert (asyncio.run(db.stats(BORIS)))["cached"] == 1
    assert (asyncio.run(db.stats()))["cached"] == 3, "администратору видно всё"
