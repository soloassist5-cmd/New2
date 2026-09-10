"""Команды в личке с ботом: доступ только владельцу."""
import asyncio
import re

import pytest

import config
import db
from core import state
from modules import botui

OWNER = 555
STRANGER = 999


class FakeBotEvent:
    def __init__(self, text, sender_id=OWNER, command="status"):
        self.sender_id = sender_id
        self.pattern_match = botui._pattern(command).match(text)
        self.replies: list[str] = []
        self.files: list[str] = []

    async def respond(self, text=None, message=None, file=None):
        if file is not None:
            self.files.append(file)
        self.replies.append(text if text is not None else (message or ""))


@pytest.fixture(autouse=True)
def env():
    config.DB_PATH.unlink(missing_ok=True)
    asyncio.run(db.init())
    state.owner_id = OWNER
    state.me = type("U", (), {"id": OWNER, "first_name": "Я", "last_name": None,
                              "username": None})()
    state.mutes.clear()
    yield
    asyncio.run(db.close())
    state.me = None


def run(handler, text, **kwargs):
    event = FakeBotEvent(text, **kwargs)
    asyncio.run(handler(event))
    return event


def test_stranger_gets_no_answer():
    event = run(botui.cmd_status, "/status", sender_id=STRANGER)
    assert event.replies == [], "чужому боту отвечать нечего"


def test_owner_sees_status():
    event = run(botui.cmd_status, "/status")
    assert "Состояние" in event.replies[0]
    assert str(OWNER) in event.replies[0]


def test_command_with_bot_username_suffix():
    """В Telegram команды приходят как /status@my_bot — это тот же вызов."""
    assert botui._pattern("status").match("/status@guard_bot") is not None


def test_deleted_reports_empty_journal():
    event = run(botui.cmd_deleted, "/deleted", command="deleted")
    assert "Пока ничего не удаляли" in event.replies[0]


def test_deleted_lists_entries():
    asyncio.run(db.add_deleted({"chat_id": 42, "msg_id": 1, "user_id": 7,
                                "text": "секрет", "media_type": None,
                                "media_ref": None, "date": db.now()}))
    event = run(botui.cmd_deleted, "/deleted 5", command="deleted")
    assert "секрет" in event.replies[0]


def test_mutes_empty_and_filled():
    event = run(botui.cmd_mutes, "/mutes", command="mutes")
    assert "пуст" in event.replies[0]

    asyncio.run(state.mute_user(42, 7, 0))
    event = run(botui.cmd_mutes, "/mutes", command="mutes")
    assert "7" in event.replies[0] and "бессрочно" in event.replies[0]


def test_unmute_requires_numeric_id():
    event = run(botui.cmd_unmute, "/unmute вася", command="unmute")
    assert "Использование" in event.replies[0]


def test_unmute_clears_every_scope():
    asyncio.run(state.mute_user(42, 7, 0))
    asyncio.run(state.mute_user(0, 7, 0))
    event = run(botui.cmd_unmute, "/unmute 7", command="unmute")
    assert "снято: **2**" in event.replies[0]
    assert not asyncio.run(state.is_muted(42, 7))


def test_unmute_on_clean_user():
    event = run(botui.cmd_unmute, "/unmute 7", command="unmute")
    assert "не был замучен" in event.replies[0]


def test_handler_errors_are_reported_not_raised():
    async def boom(_event):
        raise ValueError("сломалось")

    event = FakeBotEvent("/status")
    asyncio.run(botui.owner_only(boom)(event))
    assert "ValueError" in event.replies[0]


def test_menu_covers_every_public_command():
    menu = {name for name, _ in botui.MENU}
    handlers = {name for name, _ in botui.HANDLERS}
    assert menu <= handlers, "в меню не должно быть несуществующих команд"
    for name in ("status", "deleted", "mutes", "unmute", "backup", "help"):
        assert name in menu


def test_help_mentions_prefix():
    event = run(botui.cmd_help, "/help", command="help")
    assert re.search(r"`\.`|`" + re.escape(config.PREFIX) + "`", event.replies[0])
