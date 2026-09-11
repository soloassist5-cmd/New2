"""Ручная чистка журнала перехваченного: подтверждение, объём, чужие кнопки."""
import asyncio

import pytest

import config
import db
from bot import clearlog, commands, dotcmd
from core import state
from tests.fakes import ALL_RIGHTS, FakeBotAPI, approve, business_message

OWNER, OTHER, VASYA, MARINA = 111, 222, 777, 888
BIZ = "biz1"
DAY = 86400


@pytest.fixture(autouse=True)
def env():
    from core import chatprefs

    config.DB_PATH.unlink(missing_ok=True)
    asyncio.run(db.init())
    chatprefs.invalidate_all()
    state.users.clear()
    state.business.clear()
    state.forget_own_deletions()
    config.OWNER_ID = OWNER
    approve(OWNER, OWNER)
    approve(OTHER, OTHER)
    state.business[BIZ] = {"user_id": OWNER, "user_chat_id": OWNER,
                           "is_enabled": True, "rights": dict(ALL_RIGHTS)}
    state.api = FakeBotAPI()
    yield state.api
    asyncio.run(db.close())
    state.api = None
    state.users.clear()
    state.business.clear()
    config.OWNER_ID = 0


def put(chat_id, *, owner=OWNER, age_days=0, text="перехвачено"):
    """Кладёт запись в журнал, состарив её на нужное число дней."""
    asyncio.run(db.add_intercepted({
        "owner_id": owner, "chat_id": chat_id, "msg_id": 1, "user_id": chat_id,
        "user_name": "Собеседник", "text": text, "date": db.now(),
    }, "mute"))
    if age_days:
        asyncio.run(db.execute(
            "UPDATE intercepted SET at=? WHERE id=(SELECT MAX(id) FROM intercepted)",
            (db.now() - age_days * DAY,)))


def run(text, from_id=OWNER):
    asyncio.run(commands.handle(state.api, {
        "message_id": 1, "date": 1700000000,
        "chat": {"id": from_id, "type": "private"},
        "from": {"id": from_id, "first_name": "Кто-то"},
        "text": text}))
    return state.api


def press(data, from_id=OWNER):
    asyncio.run(commands.handle_callback(state.api, {
        "id": "q1", "data": data,
        "from": {"id": from_id, "first_name": "Кто-то"},
        "message": {"message_id": 500, "chat": {"id": from_id}}}))
    return state.api


def left(owner=OWNER) -> int:
    return asyncio.run(db.intercepted_in_scope(owner))


def buttons(api) -> list[str]:
    markup = api.markups[-1] or {"inline_keyboard": []}
    return [b["callback_data"] for row in markup["inline_keyboard"] for b in row]


# ----------------------------------------------------------- предложение ---

def test_empty_journal_offers_nothing_to_press():
    api = run("/clearlog")
    assert "пуст" in api.texts[-1]
    assert api.markups[-1] is None, "нечего чистить — не надо и кнопки"


def test_offer_shows_what_is_stored_and_waits_for_a_press():
    put(VASYA)
    put(MARINA)
    api = run("/clearlog")
    assert "**2**" in api.texts[-1] and "**2** чатов" in api.texts[-1]
    assert f"cl:a:{OWNER}" in buttons(api) and "cl:x" in buttons(api)
    assert left() == 2, "до нажатия ничего не удаляется"


def test_age_button_appears_only_when_there_is_something_old():
    put(VASYA)
    assert not [b for b in buttons(run("/clearlog")) if b.startswith("cl:d:")]

    put(MARINA, age_days=40)
    assert f"cl:d:30:{OWNER}" in buttons(run("/clearlog"))


# ------------------------------------------------------------ выполнение ---

def test_pressing_all_clears_everything_of_that_owner_only():
    put(VASYA)
    put(MARINA)
    put(VASYA, owner=OTHER)
    api = press(f"cl:a:{OWNER}")
    assert left() == 0
    assert left(OTHER) == 1, "чужой журнал не трогаем"
    assert "Удалено: **2**" in api.edits[-1][2]


def test_cancel_deletes_nothing():
    put(VASYA)
    api = press("cl:x")
    assert left() == 1
    assert "отменена" in api.edits[-1][2]


def test_someone_elses_button_does_nothing():
    """Кнопка живёт в сообщении и может уехать пересылкой — сверяем нажавшего."""
    put(VASYA)
    api = press(f"cl:a:{OWNER}", from_id=OTHER)
    assert left() == 1
    assert api.callbacks[-1][1] == clearlog.NOT_YOURS
    assert not api.edits, "чужое нажатие не должно править карточку"


def test_card_is_replaced_so_the_button_cannot_be_pressed_twice():
    put(VASYA)
    api = press(f"cl:a:{OWNER}")
    assert api.edits, "карточку заменяем итогом"
    assert api.edit_markups[-1] == {"inline_keyboard": []}, "кнопок не остаётся"


def test_days_scope_keeps_the_fresh_ones():
    put(VASYA, age_days=40)
    put(MARINA, age_days=1)
    run("/clearlog 30")
    press(f"cl:d:30:{OWNER}")
    rows = asyncio.run(db.intercepted(OWNER, limit=10))
    assert [row["chat_id"] for row in rows] == [MARINA]


def test_chat_scope_keeps_the_other_chats():
    put(VASYA)
    put(MARINA)
    api = run(f"/clearlog chat {VASYA}")
    assert f"cl:c:{VASYA}:{OWNER}" in buttons(api)
    press(f"cl:c:{VASYA}:{OWNER}")
    rows = asyncio.run(db.intercepted(OWNER, limit=10))
    assert [row["chat_id"] for row in rows] == [MARINA]


def test_clearing_asks_for_a_backup_so_it_does_not_come_back():
    """Диск на хостинге временный: без копии чистка откатится при восстановлении."""
    from core import backup

    put(VASYA)
    calls = []
    real = backup.request_soon
    try:
        backup.request_soon = lambda: calls.append(1)
        press(f"cl:a:{OWNER}")
    finally:
        backup.request_soon = real
    assert calls, "после чистки копию базы надо обновить"


# ------------------------------------------------------- из самой переписки -

def test_dot_command_offers_only_this_chat():
    put(VASYA)
    put(MARINA)
    api = state.api
    asyncio.run(dotcmd.handle(api, business_message(".clearlog", chat_id=VASYA,
                                                    from_id=OWNER), BIZ, OWNER))
    assert api.all_deleted_ids == [10], "команда убрана из переписки"
    assert api.texts_to(OWNER), "предложение приходит в личку с ботом"
    assert f"cl:c:{VASYA}:{OWNER}" in buttons(api)
    assert left() == 2, "до подтверждения ничего не удалено"


def test_dot_command_says_when_the_chat_is_clean():
    put(MARINA)
    api = state.api
    asyncio.run(dotcmd.handle(api, business_message(".clearlog", chat_id=VASYA,
                                                    from_id=OWNER), BIZ, OWNER))
    assert "ничего нет" in api.texts_to(OWNER)[-1]
    assert api.markups[-1] is None


# ---------------------------------------------------------------- разбор ---

@pytest.mark.parametrize("raw,want", [
    ("", None),
    ("   ", None),
    ("7", ("days", 7)),
    ("30", ("days", 30)),
    ("0", None),                       # ноль дней — это «всё», пусть жмут кнопку
    ("99999", None),                   # не срок, а опечатка
    ("chat -100500", ("chat", -100500)),
    ("чат 777", ("chat", 777)),
    ("chat", None),
    ("вчера", None),
])
def test_parse_args(raw, want):
    assert clearlog.parse_args(raw) == want


def test_reading_the_journal_never_deletes():
    put(VASYA)
    run("/intercepted")
    assert left() == 1
