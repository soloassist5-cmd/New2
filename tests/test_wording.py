"""Что видит посторонний человек.

Бот раздаётся другим людям, поэтому в его ответах не должно быть ни
внутренностей (размер общей базы, счётчики кэша, имена исключений), ни
инструкций, адресованных администратору.
"""
import asyncio

import pytest

import config
import db
from bot import business, commands, dotcmd
from core import chatprefs, state
from tests.fakes import ALL_RIGHTS, FakeBotAPI, approve, business_message

ADMIN, USER, PEER = 111, 555, 777
BIZ = "biz-user"

INTERNALS = ("кэш", "КБ", "МБ", "Business", "owner_id", "sqlite", "Traceback")


@pytest.fixture(autouse=True)
def env():
    config.DB_PATH.unlink(missing_ok=True)
    asyncio.run(db.init())
    chatprefs.invalidate_all()
    config.OWNER_ID = ADMIN
    config.PURGE_DEBOUNCE_SEC = 0
    state.users.clear()
    state.business.clear()
    state.mutes.clear()
    state.allowlist.clear()
    state.forget_own_deletions()
    approve(ADMIN, ADMIN, name="Админ")
    approve(USER, USER, name="Иван")
    state.bot_user = {"username": "guard_bot"}
    state.business[BIZ] = {"user_id": USER, "user_chat_id": USER,
                           "is_enabled": True, "rights": dict(ALL_RIGHTS)}
    state.api = FakeBotAPI()
    yield
    config.OWNER_ID = 0
    asyncio.run(db.close())
    state.api = None
    state.users.clear()
    state.business.clear()


def bot_command(text, user_id=USER):
    asyncio.run(commands.handle(state.api, {
        "message_id": 1, "date": 1700000000,
        "chat": {"id": user_id, "type": "private"},
        "from": {"id": user_id, "first_name": "Кто-то"}, "text": text}))
    return state.api.texts_to(user_id)[-1]


def chat_command(text, user_id=USER, msg_id=5):
    asyncio.run(dotcmd.handle(
        state.api,
        business_message(text, connection_id=BIZ, chat_id=PEER, from_id=user_id,
                         message_id=msg_id),
        BIZ, user_id))
    return state.api.texts_to(user_id)[-1]


# --------------------------------------------------------- без внутренностей

def test_user_status_hides_the_shared_database():
    """Размер общей базы — чужие данные, показывать его пользователю незачем."""
    answer = bot_command("/status")
    assert not any(word in answer for word in INTERNALS)
    assert "пользователей" not in answer


def test_admin_status_may_show_the_details():
    answer = bot_command("/status", user_id=ADMIN)
    assert "пользователей" in answer and "база" in answer


def test_chat_stats_show_only_your_own_numbers():
    answer = chat_command(".stats")
    assert not any(word in answer for word in INTERNALS)
    assert "удалённых сообщений" in answer


def test_ping_speaks_plainly():
    answer = chat_command(".ping")
    assert "аптайм" not in answer and "На связи" in answer


# ------------------------------------------------- админское — администратору

def test_botfather_and_owner_id_are_not_shown_to_users():
    state.business.clear()
    answer = bot_command("/start")
    assert "BotFather" not in answer and "Business Mode" not in answer
    assert "OWNER_ID" not in answer


def test_admin_only_commands_are_invisible_to_users():
    for command in sorted(commands.ADMIN_ONLY):
        assert f"/{command}" not in commands.HELP, command
    assert "/users" in commands.ADMIN_HELP


# ------------------------------------------------------- справка совпадает --

def test_help_does_not_promise_missing_commands():
    import re

    mentioned = set(re.findall(r"/(\w+)", commands.HELP + commands.ADMIN_HELP))
    assert mentioned <= set(commands.HANDLERS)

    in_chat = set(re.findall(r"`\.(\w+)`", commands.IN_CHAT_HINT))
    assert in_chat <= set(dotcmd.REGISTRY)


def test_help_is_not_printed_twice():
    answer = bot_command("/help")
    assert answer.count("В самих переписках") == 0, "подробный список идёт следом"
    assert ".mute" in answer


def test_no_debug_commands_survive():
    """Сырой дамп объекта и панель отладки посторонним ни к чему."""
    assert "json" not in dotcmd.REGISTRY
    for gone in ("menu", "why", "debug", "settings"):
        assert gone not in commands.HANDLERS, gone


def test_buttons_only_where_they_are_needed():
    """Кнопки остались одни — «принять/отклонить» под заявкой на доступ."""
    asyncio.run(commands.handle_callback(state.api, {
        "id": "cb", "data": "m:home", "from": {"id": USER},
        "message": {"message_id": 1, "chat": {"id": USER}}}))
    assert state.api.edits == [], "панели больше нет"
    assert state.api.callbacks, "но всплывашку закрываем"


# ------------------------------------------------------------- ошибки ------

def test_errors_do_not_leak_python():
    @dotcmd.bizcmd("kaboom", desc="тест")
    async def _kaboom(_ctx):
        raise RuntimeError("внутренности")

    try:
        answer = chat_command(".kaboom", msg_id=9)
        assert "RuntimeError" not in answer and "внутренности" not in answer
        assert "Не получилось" in answer
    finally:
        dotcmd.REGISTRY.pop("kaboom", None)
        dotcmd.COMMANDS[:] = [c for c in dotcmd.COMMANDS if c.name != "kaboom"]


def test_dnd_notice_tells_how_to_switch_off_without_buttons():
    asyncio.run(state.set_dnd(USER))
    state.dnd_notified.clear()
    state.api.sent.clear()
    asyncio.run(business.on_business_message(state.api, business_message(
        "привет", connection_id=BIZ, chat_id=PEER, from_id=PEER, message_id=20)))
    notice = [t for t in state.api.texts_to(USER) if "работает" in t][-1]
    assert "/ungmute" in notice
    assert not any(m for m in state.api.markups), "без кнопок"
