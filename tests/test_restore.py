"""Восстановление базы из закреплённого в личке бэкапа."""
import asyncio

import pytest

import config
import db
from core import backup, state
from tests.fakes import FakeBotAPI

OWNER = 111


@pytest.fixture(autouse=True)
def env():
    config.DB_PATH.unlink(missing_ok=True)
    config.DB_PATH.with_suffix(".incoming").unlink(missing_ok=True)
    state.api = FakeBotAPI()
    state.owner_chat_id = OWNER
    state.client = None
    state.log_entity = None
    yield
    state.api = None
    config.DB_PATH.unlink(missing_ok=True)


def snapshot_bytes() -> bytes:
    """Готовая база с одним мутом внутри."""
    async def build():
        await db.init()
        await state.mute_user(5, 7, 0)
        path = await backup.make_backup()
        payload = path.read_bytes()
        path.unlink()
        await db.close()
        return payload

    payload = asyncio.run(build())
    config.DB_PATH.unlink(missing_ok=True)
    state.mutes.clear()
    return payload


def pin(document: dict | None):
    state.api.chat = {"id": OWNER, "type": "private"}
    if document is not None:
        state.api.chat["pinned_message"] = {"message_id": 9, "document": document}


def test_database_returns_from_the_pinned_backup():
    """История чата боту недоступна — закреплённое сообщение единственная зацепка."""
    payload = snapshot_bytes()
    state.api.downloads["documents/BQ1"] = payload
    pin({"file_id": "BQ1", "file_name": "guard.sqlite3", "file_size": len(payload)})

    assert asyncio.run(backup.restore_from_pinned(state.api, OWNER)) is True
    assert config.DB_PATH.exists()

    async def check():
        await db.init()
        await state.load_mutes()
        assert await state.is_muted(5, 7)
        await db.close()

    asyncio.run(check())


def test_no_pinned_message_is_not_an_error():
    pin(None)
    assert asyncio.run(backup.restore_from_pinned(state.api, OWNER)) is False
    assert not config.DB_PATH.exists()


def test_pinned_note_without_a_file_is_skipped():
    state.api.chat = {"id": OWNER, "pinned_message": {"message_id": 9,
                                                      "text": "просто заметка"}}
    assert asyncio.run(backup.restore_from_pinned(state.api, OWNER)) is False


def test_foreign_pinned_file_is_refused():
    state.api.downloads["documents/BQ2"] = b"not a database"
    pin({"file_id": "BQ2", "file_name": "guard.sqlite3", "file_size": 14})
    assert asyncio.run(backup.restore_from_pinned(state.api, OWNER)) is False
    assert not config.DB_PATH.exists(), "мусор не должен становиться базой"
    assert not config.DB_PATH.with_suffix(".incoming").exists()


def test_unrelated_document_is_ignored():
    pin({"file_id": "BQ3", "file_name": "photo.zip", "file_size": 10})
    assert asyncio.run(backup.restore_from_pinned(state.api, OWNER)) is False


def test_without_owner_id_there_is_nothing_to_look_at():
    assert asyncio.run(backup.restore_from_pinned(state.api, 0)) is False


def test_unreachable_chat_is_survived():
    state.api.chat = None            # getChat падает
    assert asyncio.run(backup.restore_from_pinned(state.api, OWNER)) is False


def test_backup_pins_the_fresh_copy_and_unpins_the_old():
    async def scenario():
        await db.init()
        await db.kv_set(backup.KV_PINNED, "42")
        assert await backup.upload() is True
        assert state.api.pinned, "свежая копия закреплена"
        assert state.api.unpinned == [42], "старое закрепление снято"
        assert await db.kv_get(backup.KV_PINNED) == str(state.api.pinned[-1])
        await db.close()

    asyncio.run(scenario())
