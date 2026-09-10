"""Кэш сообщений, журнал удалённых, муты и настройки чатов."""
import asyncio

import pytest

import db
from core import backup, state

OWNER = 111


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def fresh_db():
    import config
    from modules.antidelete import invalidate_all

    config.DB_PATH.unlink(missing_ok=True)
    run(db.init())
    invalidate_all()
    state.forget_own_deletions()
    yield
    run(db.close())


def test_cache_and_lookup():
    async def scenario():
        await db.cache_message(owner_id=OWNER, chat_id=42, msg_id=7, user_id=5, is_private=True,
                               text="привет", media_type=None, media_ref=None,
                               reply_to=None, date=db.now())
        row = await db.get_message(OWNER, 42, 7)
        assert row["text"] == "привет" and row["is_private"] == 1
        # В личках событие удаления не содержит chat_id — ищем по одному msg_id.
        assert (await db.find_private_message(OWNER, 7))["chat_id"] == 42
        assert await db.find_private_message(OWNER, 999) is None
    run(scenario())


def test_media_ref_is_not_lost_on_reupsert():
    async def scenario():
        args = {"owner_id": OWNER, "chat_id": 1, "msg_id": 2, "user_id": 3,
                "is_private": True, "text": "a", "media_type": "фото",
                "reply_to": None, "date": db.now()}
        await db.cache_message(media_ref=None, **args)
        await db.set_media_ref(OWNER, 1, 2, 555)
        await db.cache_message(media_ref=None, **{**args, "text": "b"})
        row = await db.get_message(OWNER, 1, 2)
        assert row["text"] == "b" and row["media_ref"] == 555
    run(scenario())


def test_deleted_journal():
    async def scenario():
        await db.cache_message(owner_id=OWNER, chat_id=42, msg_id=7, user_id=5, is_private=True,
                               text="пока", media_type=None, media_ref=None,
                               reply_to=None, date=db.now())
        await db.add_deleted(dict(await db.get_message(OWNER, 42, 7)) | {"owner_id": OWNER})
        rows = await db.last_deleted(OWNER, 42, 10)
        assert len(rows) == 1 and rows[0]["text"] == "пока"
        assert len(await db.last_deleted(OWNER, None, 10)) == 1
        assert await db.last_deleted(OWNER, 999, 10) == []
    run(scenario())


def test_mutes_scope_and_expiry():
    async def scenario():
        await state.mute_user(OWNER, 42, 5, 0)
        assert await state.is_muted(OWNER, 42, 5)
        assert not await state.is_muted(OWNER, 43, 5), "мут чата не должен течь в другие чаты"

        await state.mute_user(OWNER, 0, 6, 0)
        assert await state.is_muted(OWNER, -100999, 6), "глобальный мут действует везде"

        await state.mute_user(OWNER, 1, 7, db.now() - 5)
        assert not await state.is_muted(OWNER, 1, 7)
        assert (OWNER, 1, 7) not in state.mutes, "протухший мут снимается автоматически"

        assert await state.unmute_user(OWNER, 42, 5)
        assert not await state.unmute_user(OWNER, 42, 5)
    run(scenario())


def test_mutes_survive_restart():
    async def scenario():
        await state.mute_user(OWNER, 42, 5, 0, "спам")
        state.mutes.clear()
        await state.load_mutes()
        assert await state.is_muted(OWNER, 42, 5)
    run(scenario())


def test_settings_upsert_keeps_siblings():
    async def scenario():
        await db.set_setting(OWNER, 42, "antidelete", 0)
        await db.set_setting(OWNER, 42, "ignored", 1)
        row = await db.get_settings(OWNER, 42)
        assert row["antidelete"] == 0 and row["ignored"] == 1
    run(scenario())


def test_chat_flag_defaults():
    async def scenario():
        from core import chatprefs
        chatprefs.invalidate_all()
        assert (await chatprefs.flags(OWNER, 777, True))["antidelete"] is True
        assert (await chatprefs.flags(OWNER, -100777, False))["antidelete"] is False
    run(scenario())


def test_notes_are_case_insensitive():
    async def scenario():
        await db.save_note(OWNER, 42, "Привет", "текст")
        assert (await db.get_note(OWNER, 42, "привет"))["content"] == "текст"
        await db.drop_note(OWNER, 42, "ПРИВЕТ")
        assert await db.get_note(OWNER, 42, "привет") is None
    run(scenario())


def test_cleanup_respects_ttl():
    async def scenario():
        import config
        old = db.now() - (config.CACHE_TTL_HOURS + 1) * 3600
        await db.cache_message(owner_id=OWNER, chat_id=1, msg_id=1, user_id=1, is_private=True,
                               text="старое", media_type=None, media_ref=None,
                               reply_to=None, date=old)
        await db.cache_message(owner_id=OWNER, chat_id=1, msg_id=2, user_id=1, is_private=True,
                               text="свежее", media_type=None, media_ref=None,
                               reply_to=None, date=db.now())
        removed, _ = await db.cleanup()
        assert removed == 1
        assert await db.get_message(OWNER, 1, 1) is None
        assert await db.get_message(OWNER, 1, 2) is not None
    run(scenario())


def test_backup_file_is_created():
    async def scenario():
        await db.cache_message(owner_id=OWNER, chat_id=1, msg_id=1, user_id=1, is_private=True,
                               text="x", media_type=None, media_ref=None,
                               reply_to=None, date=db.now())
        path = await backup.make_backup()
        assert path is not None and path.stat().st_size > 0
        path.unlink()
    run(scenario())


def test_own_deletions_are_scoped_to_owner_and_chat():
    state.mark_own_deletion(OWNER, 42, 7, private=True)
    assert state.was_own_deletion(OWNER, 42, 7)
    assert state.was_own_deletion(OWNER, None, 7), "в личке chat_id отсутствует"
    assert not state.was_own_deletion(OWNER, 42, 8)

    state.mark_own_deletion(OWNER, -100500, 11)          # группа: без алиаса
    assert state.was_own_deletion(OWNER, -100500, 11)
    assert not state.was_own_deletion(OWNER, -100999, 11), "id чатов пересекаются"
    assert not state.was_own_deletion(999, -100500, 11), "и владельцы тоже"
