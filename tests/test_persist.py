"""Настройки не должны теряться при обновлении бота.

Хранятся они в базе, а не в памяти — в памяти только читаемая копия. Но файл
базы на бесплатном хостинге временный, поэтому важна не сама запись, а то,
насколько быстро она попадает в копию, которую бот кладёт себе в личку.
"""
import asyncio

import pytest

import config
import db
from core import backup, chatprefs, state
from tests.fakes import FakeBotAPI, approve

OWNER, PEER = 111, 777


@pytest.fixture(autouse=True)
def env():
    config.DB_PATH.unlink(missing_ok=True)
    config.OWNER_ID = OWNER
    config.BACKUP_EVERY_MIN = 60
    config.BACKUP_SOON_SEC = 1
    asyncio.run(db.init())
    chatprefs.invalidate_all()
    state.users.clear()
    state.mutes.clear()
    state.allowlist.clear()
    approve(OWNER, OWNER)
    state.api = FakeBotAPI()
    state.client = None
    state.log_entity = None
    backup.forget_soon()
    yield
    backup.forget_soon()
    config.OWNER_ID = 0
    config.BACKUP_EVERY_MIN = 0
    asyncio.run(db.close())
    state.api = None


def test_settings_live_in_the_database_not_in_memory():
    async def scenario():
        await state.allow_user(OWNER, PEER, "Вася")
        await chatprefs.toggle(OWNER, 42, "ignored", True, title="Петя")
        await state.mute_user(OWNER, 5, 9, 0)

        state.allowlist.clear()          # как будто процесс перезапустился
        state.mutes.clear()
        chatprefs.invalidate_all()
        await state.load_allowlist()
        await state.load_mutes()

        assert state.is_allowed(OWNER, PEER)
        assert await state.is_muted(OWNER, 5, 9)
        assert (await chatprefs.flags(OWNER, 42, True))["ignored"] is True
    asyncio.run(scenario())


@pytest.mark.parametrize("change", [
    "allow", "deny", "mute", "unmute", "dnd_on", "dnd_off", "chat", "reset",
    "access",
])
def test_every_settings_change_asks_for_a_fresh_copy(change):
    """Расписание раз в час — слишком редко: передеплой может случиться раньше."""
    actions = {
        "allow": lambda: state.allow_user(OWNER, PEER, "Вася"),
        "deny": lambda: state.deny_user(OWNER, PEER),
        "mute": lambda: state.mute_user(OWNER, 5, 9, 0),
        "unmute": lambda: state.unmute_user(OWNER, 5, 9),
        "dnd_on": lambda: state.set_dnd(OWNER),
        "dnd_off": lambda: state.clear_dnd(OWNER),
        "chat": lambda: chatprefs.toggle(OWNER, 42, "ignored", True),
        "reset": lambda: chatprefs.reset(OWNER, 42),
        "access": lambda: state.remember_user(999, status=db.APPROVED),
    }
    prepare = {"deny": "allow", "unmute": "mute"}.get(change)

    async def scenario():
        if prepare:
            await actions[prepare]()
            backup.forget_soon()
        await actions[change]()
        assert backup._soon_task is not None and not backup._soon_task.done()
    asyncio.run(scenario())


def test_repeated_changes_do_not_pile_up_copies():
    async def scenario():
        await state.allow_user(OWNER, 1, "раз")
        first = backup._soon_task
        await state.allow_user(OWNER, 2, "два")
        await state.mute_user(OWNER, 5, 9, 0)
        assert backup._soon_task is first, "подряд идущие правки объединяются"
    asyncio.run(scenario())


def test_the_copy_actually_gets_sent_and_pinned():
    async def scenario():
        await state.allow_user(OWNER, PEER, "Вася")
        await asyncio.sleep(config.BACKUP_SOON_SEC + 0.3)
        assert state.api.files, "копия ушла владельцу"
        assert state.api.pinned, "и закреплена — иначе её не найти при старте"
    asyncio.run(scenario())


def test_nothing_is_scheduled_when_backups_are_off():
    config.BACKUP_EVERY_MIN = 0

    async def scenario():
        await state.allow_user(OWNER, PEER, "Вася")
        assert backup._soon_task is None
    asyncio.run(scenario())


def test_caching_a_message_does_not_trigger_a_copy():
    """Сообщения идут потоком: копия на каждое превратилась бы в флуд."""
    async def scenario():
        await db.cache_message(owner_id=OWNER, chat_id=PEER, msg_id=1, user_id=PEER,
                               is_private=True, text="привет", media_type=None,
                               media_ref=None, reply_to=None, date=db.now())
        assert backup._soon_task is None
    asyncio.run(scenario())


def test_settings_survive_a_full_redeploy():
    """Сквозная проверка: правка → копия → пустой диск → восстановление."""
    async def scenario():
        await state.allow_user(OWNER, PEER, "Вася")
        await chatprefs.toggle(OWNER, 42, "ignored", True, title="Петя")
        await asyncio.sleep(config.BACKUP_SOON_SEC + 0.3)
        payload = state.api.uploads[-1]

        await db.close()                      # передеплой: диск пуст, память чиста
        config.DB_PATH.unlink()
        state.allowlist.clear()
        state.mutes.clear()
        chatprefs.invalidate_all()

        state.api.chat = {"id": OWNER, "pinned_message": {
            "message_id": state.api.pinned[-1],
            "document": {"file_id": "B1", "file_name": "guard.sqlite3",
                         "file_size": len(payload)}}}
        state.api.downloads["documents/B1"] = payload
        assert await backup.restore_from_pinned(state.api, OWNER)

        await db.init()
        await state.load_allowlist()
        assert state.is_allowed(OWNER, PEER), "белый список вернулся"
        assert (await chatprefs.flags(OWNER, 42, True))["ignored"] is True
    asyncio.run(scenario())


# ------------------------------------------- копия при остановке процесса ---

def test_a_scheduled_backup_is_finished_on_shutdown():
    """Отменить её значило бы выбросить правку, сделанную минуту назад."""
    from core import backup

    async def scenario():
        done = []
        real = backup.upload

        async def fake_upload():
            done.append(1)
            return True

        backup.upload = fake_upload
        try:
            config.BACKUP_EVERY_MIN = 30
            config.BACKUP_SOON_SEC = 60          # сама бы ушла через минуту
            backup.request_soon()
            await backup.flush_soon()            # а процесс гасят сейчас
        finally:
            backup.upload = real
            backup.forget_soon()
        return done

    assert asyncio.run(scenario()), "копия при остановке должна успеть уйти"


def test_flush_is_quiet_when_nothing_was_scheduled():
    from core import backup

    async def scenario():
        calls = []
        real = backup.upload

        async def fake_upload():
            calls.append(1)
            return True

        backup.upload = fake_upload
        try:
            backup.forget_soon()
            await backup.flush_soon()
        finally:
            backup.upload = real
        return calls

    assert asyncio.run(scenario()) == []


def test_save_now_does_not_wait_and_drops_the_scheduled_one():
    from core import backup

    async def scenario():
        calls = []
        real = backup.upload

        async def fake_upload():
            calls.append(1)
            return True

        backup.upload = fake_upload
        try:
            config.BACKUP_EVERY_MIN = 30
            config.BACKUP_SOON_SEC = 60
            backup.request_soon()
            await backup.save_now()
            pending = backup._soon_task
        finally:
            backup.upload = real
            backup.forget_soon()
        return calls, pending

    calls, pending = asyncio.run(scenario())
    assert calls == [1], "ровно одна загрузка, без ожидания"
    assert pending is None, "запланированная снимается — она уже не нужна"
