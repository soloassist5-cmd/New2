"""Режим Telegram Business: подключение, кэш, перехват и отчёты об удалении."""
import asyncio
import json

import pytest

import config
import db
from bot import business
from core import state
from tests.fakes import ALL_RIGHTS, FakeBotAPI, business_message

OWNER = 111
OWNER_CHAT = 111
PEER = 777
BIZ = "biz1"


def connection(rights=None, enabled=True):
    return {
        "id": BIZ,
        "user": {"id": OWNER, "first_name": "Владелец"},
        "user_chat_id": OWNER_CHAT,
        "is_enabled": enabled,
        "rights": rights if rights is not None else dict(ALL_RIGHTS),
    }


@pytest.fixture(autouse=True)
def env():
    from modules.antidelete import invalidate_all

    config.DB_PATH.unlink(missing_ok=True)
    asyncio.run(db.init())
    invalidate_all()          # кэш настроек живёт в модуле и переживает пересоздание БД
    config.PURGE_DEBOUNCE_SEC = 0     # в тестах разбираем удаления сразу
    business._pending.clear()
    state.business.clear()
    state.mutes.clear()
    state.forget_own_deletions()
    asyncio.run(state.load_dnd())        # состояние режимов живёт
    asyncio.run(state.load_allowlist())  # в модуле, а не только в БД
    state.client = None
    state.log_entity = None
    state.owner_id = 0
    state.owner_chat_id = 0
    state.api = FakeBotAPI()
    yield
    asyncio.run(db.close())
    state.api = None
    state.business.clear()


def connect(**kwargs):
    asyncio.run(business.on_business_connection(connection(**kwargs)))
    return state.api


def incoming(**kwargs):
    asyncio.run(business.on_business_message(state.api, business_message(**kwargs)))


# --------------------------------------------------------------- подключение

def test_connection_is_stored_and_confirmed():
    api = connect()
    assert state.business[BIZ]["user_id"] == OWNER
    assert state.owner_id == OWNER and state.owner_chat_id == OWNER_CHAT
    assert "подключён" in api.texts[0]
    rows = asyncio.run(db.all_business())
    assert len(rows) == 1 and json.loads(rows[0]["rights"])["can_reply"] is True


def test_connection_reports_missing_rights():
    api = connect(rights={"can_read_messages": True, "can_reply": True})
    report = api.texts[0]
    assert "⚠️" in report and "не работает `.mute`" in report


def test_disconnection_is_reported():
    connect()
    api = connect(enabled=False)
    assert "отключён" in api.texts[-1].lower()
    assert not state.business[BIZ]["is_enabled"]


def test_connections_survive_restart():
    connect()
    state.business.clear()
    state.owner_id = state.owner_chat_id = 0
    asyncio.run(business.load_connections())
    assert state.business[BIZ]["rights"]["can_delete_all_messages"] is True
    assert state.owner_id == OWNER


def test_legacy_connection_without_rights_object():
    """До Bot API 9.0 приходил только can_reply."""
    payload = connection()
    payload.pop("rights")
    payload["can_reply"] = True
    asyncio.run(business.on_business_connection(payload))
    assert state.business[BIZ]["rights"]["can_reply"] is True


# --------------------------------------------------------------------- кэш --

def test_incoming_message_is_cached():
    connect()
    incoming(text="привет", message_id=42)
    row = asyncio.run(db.get_message(PEER, 42))
    assert row["text"] == "привет" and row["business_id"] == BIZ
    assert row["user_name"] == "Собеседник"


def test_photo_is_cached_with_file_id():
    connect()
    incoming(text="подпись", message_id=43, photo="AgACphoto")
    row = asyncio.run(db.get_message(PEER, 43))
    assert row["media_type"] == "фото" and row["file_id"] == "AgACphoto"
    assert row["text"] == "подпись", "подпись к фото — тоже текст сообщения"


def test_ignored_chat_is_not_cached():
    connect()
    asyncio.run(db.set_setting(PEER, "ignored", 1))
    from modules.antidelete import invalidate
    invalidate(PEER)
    incoming(text="привет", message_id=44)
    assert asyncio.run(db.get_message(PEER, 44)) is None


# ------------------------------------------------------------------- мут ----

def test_muted_message_is_deleted_silently_but_saved():
    """Замученный собеседник не должен превращать личку с ботом в ту же переписку."""
    api = connect()
    api.sent.clear()
    asyncio.run(state.mute_user(PEER, PEER, 0))
    incoming(text="спам", message_id=50)

    assert api.deleted == [(BIZ, [50])]
    assert api.sent == [], "мгновенных уведомлений быть не должно"
    assert state.was_own_deletion(PEER, 50), "своё удаление не идёт в журнал удалений"

    saved = asyncio.run(db.intercepted(PEER))
    assert len(saved) == 1 and saved[0]["text"] == "спам"
    assert saved[0]["reason"] == "mute"


def test_live_notifications_can_be_turned_back_on():
    config.MUTE_LOG = True
    try:
        api = connect()
        asyncio.run(state.mute_user(PEER, PEER, 0))
        incoming(text="спам", message_id=50)
        assert any("Перехвачено" in text for text in api.texts)
    finally:
        config.MUTE_LOG = False


def test_intercepted_media_is_saved_with_file_id():
    api = connect()
    asyncio.run(state.mute_user(PEER, PEER, 0))
    incoming(text="", message_id=51, photo="AgACspam")
    saved = asyncio.run(db.intercepted(PEER))
    assert saved[0]["file_id"] == "AgACspam" and saved[0]["media_type"] == "фото"
    assert api.media == [], "картинку тоже не шлём сразу"


def test_missing_delete_right_is_explained_once():
    state.api = FakeBotAPI(delete_ok=False)
    api = connect(rights={"can_read_messages": True, "can_reply": True})
    asyncio.run(state.mute_user(PEER, PEER, 0))
    incoming(text="спам", message_id=52)
    assert any("нет права удалять" in text for text in api.texts)


def test_owner_messages_are_never_intercepted():
    api = connect()
    asyncio.run(state.mute_user(PEER, OWNER, 0))
    incoming(text="моё", message_id=53, from_id=OWNER)
    assert api.deleted == []


# -------------------------------------------------------------- удаления ----

def deleted_update(ids, chat_id=PEER):
    return {"business_connection_id": BIZ,
            "chat": {"id": chat_id, "type": "private", "first_name": "Чат"},
            "message_ids": ids}


def test_deletion_produces_a_report():
    api = connect()
    incoming(text="секрет", message_id=60)
    asyncio.run(business.on_deleted_business_messages(state.api, deleted_update([60])))
    report = api.texts[-1]
    assert "Удалённое сообщение" in report and "секрет" in report
    assert asyncio.run(db.get_message(PEER, 60)) is None, "из кэша убрали"
    assert len(asyncio.run(db.last_deleted(PEER, 5))) == 1


def test_deleted_photo_is_resent_by_file_id():
    api = connect()
    incoming(text="", message_id=61, photo="AgACgone")
    asyncio.run(business.on_deleted_business_messages(state.api, deleted_update([61])))
    assert api.media and api.media[-1][1] == "AgACgone"


def test_unknown_message_id_is_skipped():
    api = connect()
    before = len(api.sent)
    asyncio.run(business.on_deleted_business_messages(state.api, deleted_update([999])))
    assert len(api.sent) == before


def test_own_deletion_in_one_chat_does_not_silence_another():
    """В Business id сообщений нумеруются внутри чата и пересекаются между
    чатами, поэтому «своё удаление» нельзя запоминать без chat_id."""
    other = 888
    api = connect()
    incoming(text="важное", message_id=63, chat_id=other, from_id=other)
    state.mark_own_deletion(PEER, 63)          # своё удаление в другом чате
    asyncio.run(business.on_deleted_business_messages(
        state.api, deleted_update([63], chat_id=other)))
    assert any("важное" in text for text in api.texts)


def test_own_deletions_do_not_produce_reports():
    api = connect()
    incoming(text="спам", message_id=62)
    state.mark_own_deletion(PEER, 62, private=True)
    before = len(api.sent)
    asyncio.run(business.on_deleted_business_messages(state.api, deleted_update([62])))
    assert len(api.sent) == before


# ---------------------------------------------------------------- правки ----

def test_edit_is_reported_with_both_versions():
    api = connect()
    incoming(text="было", message_id=70)
    edited = business_message(text="стало", message_id=70)
    asyncio.run(business.on_edited_business_message(state.api, edited))
    report = api.texts[-1]
    assert "Было:" in report and "было" in report and "стало" in report
    assert asyncio.run(db.get_message(PEER, 70))["text"] == "стало"


def test_edit_without_text_change_is_silent():
    api = connect()
    incoming(text="одно и то же", message_id=71)
    before = len(api.sent)
    asyncio.run(business.on_edited_business_message(
        state.api, business_message(text="одно и то же", message_id=71)))
    assert len(api.sent) == before
