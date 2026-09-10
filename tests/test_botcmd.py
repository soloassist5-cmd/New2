"""Команды в личке с ботом и инструкция по подключению Business."""
import asyncio

import pytest

import config
import db
from bot import commands
from core import state
from tests.fakes import ALL_RIGHTS, FakeBotAPI

OWNER = 111
STRANGER = 999
BIZ = "biz1"


def message(text, *, from_id=OWNER, chat_type="private"):
    return {"message_id": 1, "date": 1700000000,
            "chat": {"id": from_id, "type": chat_type},
            "from": {"id": from_id, "first_name": "Кто-то"},
            "text": text}


@pytest.fixture(autouse=True)
def env():
    from modules.antidelete import invalidate_all

    config.DB_PATH.unlink(missing_ok=True)
    asyncio.run(db.init())
    invalidate_all()
    state.business.clear()
    state.mutes.clear()
    state.client = None
    state.owner_id = 0
    state.owner_chat_id = 0
    state.bot_user = {"username": "guard_test_bot"}
    state.api = FakeBotAPI()
    yield
    asyncio.run(db.close())
    state.api = None
    state.business.clear()


def connected():
    state.owner_id = OWNER
    state.owner_chat_id = OWNER
    state.business[BIZ] = {"user_id": OWNER, "user_chat_id": OWNER,
                           "is_enabled": True, "rights": dict(ALL_RIGHTS)}


def run(text, **kwargs):
    asyncio.run(commands.handle(state.api, message(text, **kwargs)))
    return state.api


# ----------------------------------------------------------- инструкция ----

def test_start_gives_connection_steps_when_not_connected():
    api = run("/start")
    text = api.texts[0]
    assert "Telegram Premium" in text
    assert "Чат-боты" in text
    assert "@guard_test_bot" in text, "в инструкции должен быть юзернейм самого бота"
    assert "удалять сообщения" in text


def test_connect_works_for_anyone_before_setup():
    """До подключения владельца ещё нет — инструкция публична и ничего не выдаёт."""
    api = run("/connect", from_id=STRANGER)
    assert "Как подключить" in api.texts[0]


def test_start_after_connection_shows_the_command_list():
    connected()
    api = run("/start")
    assert "/deleted" in api.texts[0] and f"{config.PREFIX}mute" in api.texts[0]


def test_start_remembers_owner_chat():
    connected()
    state.owner_chat_id = 0
    run("/start")
    assert state.owner_chat_id == OWNER


# ------------------------------------------------------------- доступ ------

def test_data_commands_require_a_connection():
    api = run("/deleted")
    assert "подключите" in api.texts[0].lower()


def test_stranger_gets_no_data():
    connected()
    api = run("/deleted", from_id=STRANGER)
    assert api.sent == [], "чужому отвечать нечего"


def test_group_messages_are_ignored():
    connected()
    api = run("/status", chat_type="supergroup")
    assert api.sent == []


def test_unknown_command_is_ignored():
    connected()
    api = run("/чегоизвольте")
    assert api.sent == []


# ------------------------------------------------------------- данные ------

def test_status_lists_rights_when_connected():
    connected()
    api = run("/status")
    text = api.texts[0]
    assert "Подключено" in text and "удалять сообщения собеседника" in text


def test_status_without_connection_points_to_connect():
    state.owner_id = OWNER          # владелец известен из OWNER_ID, но Business нет
    api = run("/status")
    assert "/connect" in api.texts[0]


def test_deleted_shows_journal():
    connected()
    asyncio.run(db.add_deleted({"chat_id": 5, "msg_id": 1, "user_id": 7,
                                "text": "секрет", "media_type": None,
                                "media_ref": None, "date": db.now(),
                                "user_name": "Вася", "file_id": None}))
    api = run("/deleted 5")
    assert "секрет" in api.texts[0] and "Вася" in api.texts[0]


def test_mutes_and_unmute():
    connected()
    asyncio.run(state.mute_user(5, 7, 0))
    asyncio.run(state.mute_user(0, 7, 0))
    api = run("/mutes")
    assert "7" in api.texts[0]

    api = run("/unmute 7")
    assert "снято: **2**" in api.texts[-1]
    assert not asyncio.run(state.is_muted(5, 7))


def test_unmute_validates_its_argument():
    connected()
    api = run("/unmute вася")
    assert "Использование" in api.texts[0]


def test_backup_sends_a_file():
    connected()
    api = run("/backup")
    assert api.files and api.files[0][1] == "guard.sqlite3"


# ------------------------------------------------ восстановление базы ------

def document_message(name="guard.sqlite3", *, from_id=OWNER, size=1024,
                     file_id="BQAC1"):
    return {"message_id": 2, "date": 1700000000,
            "chat": {"id": from_id, "type": "private"},
            "from": {"id": from_id, "first_name": "Кто-то"},
            "document": {"file_id": file_id, "file_name": name, "file_size": size}}


def test_uploaded_backup_replaces_the_database():
    """На хостах с эфемерным диском база возвращается пересылкой файла боту."""
    from core import backup

    connected()
    asyncio.run(state.mute_user(5, 7, 0))
    snapshot = asyncio.run(backup.make_backup())
    payload = snapshot.read_bytes()
    snapshot.unlink()

    asyncio.run(state.unmute_user(5, 7))          # «потеряли» состояние
    assert state.mutes == {}

    state.api.downloads["documents/BQAC1"] = payload
    asyncio.run(commands.handle(state.api, document_message()))

    assert "восстановлена" in state.api.texts[-1]
    assert asyncio.run(state.is_muted(5, 7)), "мут вернулся вместе с базой"


def test_broken_upload_never_touches_the_working_database():
    """Чужой файл не должен уничтожить рабочую базу."""
    connected()
    asyncio.run(state.mute_user(5, 7, 0))
    state.api.downloads["documents/BQAC1"] = "это не sqlite".encode()

    asyncio.run(commands.handle(state.api, document_message()))

    assert "не похоже на базу" in state.api.texts[-1]
    assert asyncio.run(db.stats())["mutes"] == 1, "прежняя база цела"
    assert not config.DB_PATH.with_suffix(".incoming").exists()


def test_foreign_document_is_ignored():
    connected()
    asyncio.run(commands.handle(state.api, document_message(from_id=STRANGER)))
    assert state.api.sent == []


def test_unrelated_file_type_is_ignored():
    connected()
    asyncio.run(commands.handle(state.api, document_message(name="photo.zip")))
    assert state.api.sent == []


def test_oversized_backup_is_refused():
    connected()
    asyncio.run(commands.handle(
        state.api, document_message(size=25 * 1024 * 1024)))
    assert "МБ" in state.api.texts[0]


def test_menu_is_published():
    asyncio.run(commands.publish_menu(state.api))
    names = {item["command"] for item in state.api.commands}
    assert {"status", "deleted", "connect", "help"} <= names
