"""Панель управления: состояние видно, всё выключается той же кнопкой."""
import asyncio

import pytest

import config
import db
from bot import business, commands, ui
from core import chatprefs, state
from tests.fakes import ALL_RIGHTS, FakeBotAPI, approve

ANNA, BORIS = 111, 222
PEER = 777
BIZ = "biz-anna"


@pytest.fixture(autouse=True)
def env():
    config.DB_PATH.unlink(missing_ok=True)
    asyncio.run(db.init())
    chatprefs.invalidate_all()
    config.OWNER_ID = ANNA
    state.users.clear()
    state.business.clear()
    state.mutes.clear()
    state.allowlist.clear()
    state.forget_own_deletions()
    business._pending.clear()
    approve(ANNA, ANNA, name="Анна")
    approve(BORIS, BORIS, name="Борис")
    state.business[BIZ] = {"user_id": ANNA, "user_chat_id": ANNA,
                           "is_enabled": True, "rights": dict(ALL_RIGHTS)}
    state.api = FakeBotAPI()
    yield
    config.OWNER_ID = 0
    asyncio.run(db.close())
    state.api = None
    state.users.clear()
    state.business.clear()


def press(data, *, from_id=ANNA, card_id=7):
    asyncio.run(commands.handle_callback(state.api, {
        "id": "cb", "data": data, "from": {"id": from_id},
        "message": {"message_id": card_id, "chat": {"id": from_id}}}))
    return state.api


def open_menu(user_id=ANNA):
    asyncio.run(commands.handle(state.api, {
        "message_id": 1, "date": 1700000000,
        "chat": {"id": user_id, "type": "private"},
        "from": {"id": user_id, "first_name": "Кто-то"}, "text": "/menu"}))
    return state.api


def buttons(markup: dict) -> list[str]:
    return [b["callback_data"] for row in markup["inline_keyboard"] for b in row]


def screen_buttons(api) -> list[str]:
    """Кнопки экрана, который бот нарисовал последним."""
    return buttons(api.edit_markups[-1])


# ------------------------------------------------------------- открытие ----

def test_menu_shows_state_and_buttons():
    api = open_menu()
    text = api.texts[0]
    assert "Не беспокоить" in text and "выключен" in text
    assert "m:dnd" in buttons(api.markups[-1])
    assert "m:chats" in buttons(api.markups[-1])


def test_menu_is_for_approved_users_only():
    api = press("m:home", from_id=999)
    assert "не открыт" in api.callbacks[-1][1]
    assert api.edits == []


# --------------------------------------------------- не беспокоить -------

def test_dnd_screen_explains_and_offers_the_switch():
    api = press("m:dnd")
    assert "выключен" in api.edits[-1][2]
    assert "dnd:on" in screen_buttons(api)
    assert config.DND_TEXT.splitlines()[0] in api.edits[-1][2], \
        "видно, что именно получит собеседник"


def test_one_button_turns_dnd_on_and_the_same_one_turns_it_off():
    """Именно этого не хватало: включил кнопкой — выключаешь ею же."""
    press("dnd:on")
    assert state.dnd_active(ANNA)
    assert "Включено" in state.api.callbacks[-1][1]
    assert "dnd:off" in screen_buttons(state.api), "кнопка сразу стала выключателем"

    press("dnd:off")
    assert not state.dnd_active(ANNA)
    assert "Выключено" in state.api.callbacks[-1][1]
    assert "dnd:on" in screen_buttons(state.api)


# ----------------------------------------------------------------- муты ---

def test_mutes_screen_lists_and_unmutes():
    asyncio.run(state.mute_user(ANNA, PEER, PEER, 0))
    press("m:mutes")
    assert f"mu:{PEER}:{PEER}" in screen_buttons(state.api)

    press(f"mu:{PEER}:{PEER}")
    assert not asyncio.run(state.is_muted(ANNA, PEER, PEER))
    assert "снят" in state.api.callbacks[-1][1].lower()


def test_empty_mutes_screen_explains_how_to_mute():
    press("m:mutes")
    assert ".mute" in state.api.edits[-1][2]


# ---------------------------------------------------------------- чаты ----

def test_ignored_chat_is_visible_and_reversible():
    """Главная жалоба: непонятно, что включено и как это выключить."""
    asyncio.run(chatprefs.toggle(ANNA, PEER, "ignored", True, title="Вася"))

    press("m:chats")
    text = state.api.edits[-1][2]
    assert "Вася" in text and "игнорирую" in text
    assert f"ch:{PEER}" in screen_buttons(state.api)

    press(f"ch:{PEER}")
    assert (asyncio.run(chatprefs.flags(ANNA, PEER, True)))["ignored"] is False
    assert "как раньше" in state.api.callbacks[-1][1]


def test_chats_screen_without_settings_explains_the_default():
    press("m:chats")
    text = state.api.edits[-1][2]
    assert "по умолчанию" in text or "Особых настроек нет" in text


# ------------------------------------------------------- белый список ----

def test_allowlist_screen_removes_entries():
    asyncio.run(state.allow_user(ANNA, PEER, "Вася"))
    press("m:allow")
    assert f"al:{PEER}" in screen_buttons(state.api)

    press(f"al:{PEER}")
    assert not state.is_allowed(ANNA, PEER)


# -------------------------------------------------------------- журнал ---

def test_journal_paginates():
    async def fill():
        for i in range(12):
            await db.add_deleted({"owner_id": ANNA, "chat_id": PEER, "msg_id": i,
                                  "user_id": PEER, "user_name": "Вася",
                                  "text": f"сообщение {i}", "media_type": None,
                                  "media_ref": None, "file_id": None,
                                  "date": 1700000000 + i})
    asyncio.run(fill())

    press("m:log:0")
    assert "m:log:8" in screen_buttons(state.api), "есть кнопка «ещё»"

    press("m:log:8")
    assert "m:log:0" in screen_buttons(state.api), "и кнопка назад по страницам"


def test_empty_journal_says_so():
    press("m:log:0")
    assert "пуст" in state.api.edits[-1][2].lower()


# ------------------------------------------- кнопки под карточкой отчёта --

def test_quick_mute_from_a_report_card():
    press(f"q:{PEER}:{PEER}")
    assert asyncio.run(state.is_muted(ANNA, PEER, PEER))
    assert "Замучен" in state.api.callbacks[-1][1]
    assert state.api.markup_edits, "клавиатура карточки заменена"
    assert f"mu:{PEER}:{PEER}" in [
        b["callback_data"]
        for row in state.api.markup_edits[-1][2]["inline_keyboard"] for b in row]


def test_quick_ignore_from_a_report_card():
    press(f"qi:{PEER}")
    assert (asyncio.run(chatprefs.flags(ANNA, PEER, True)))["ignored"] is True
    assert f"ch:{PEER}" in [
        b["callback_data"]
        for row in state.api.markup_edits[-1][2]["inline_keyboard"] for b in row]


def test_report_card_carries_the_actions():
    keyboard = ui.report_actions(PEER, PEER)
    assert [b["callback_data"] for b in keyboard["inline_keyboard"][0]] == [
        f"q:{PEER}:{PEER}", f"qi:{PEER}"]


# ------------------------------------------------------------ изоляция ---

def test_panel_shows_only_your_own_state():
    asyncio.run(state.mute_user(ANNA, PEER, PEER, 0))
    press("m:mutes", from_id=BORIS)
    assert "Никто не замучен" in state.api.edits[-1][2]


def test_dnd_button_toggles_only_your_own_mode():
    press("dnd:on", from_id=BORIS)
    assert state.dnd_active(BORIS) and not state.dnd_active(ANNA)


# ------------------------------------------------------------- справка ---

def test_help_screen_separates_the_three_modes():
    press("m:help")
    text = state.api.edits[-1][2]
    assert "Мут" in text and "Не беспокоить" in text and "Игнор чата" in text
    assert "мут удаляет чужие сообщения" in text
