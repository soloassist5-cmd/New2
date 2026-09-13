"""Отчёт по истории имён.

Историю бот пишет сам, сверяя имя в каждом сообщении с прошлым разом. Отчёт
обязан быть честным: первое имя — это знакомство, а не переименование, и
задним числом историю взять неоткуда.
"""
import asyncio

import pytest

import config
import db
from bot import commands, dotcmd, namelog
from core import state
from tests.fakes import ALL_RIGHTS, FakeBotAPI, approve, business_message

OWNER, VASYA, MARINA, OTHER = 111, 777, 888, 222
BIZ = "biz1"
DAY = 86400


@pytest.fixture(autouse=True)
def env():
    from core import chatprefs

    config.DB_PATH.unlink(missing_ok=True)
    asyncio.run(db.init())
    chatprefs.invalidate_all()
    config.OWNER_ID = OWNER
    state.users.clear()
    state.business.clear()
    state.forget_own_deletions()
    approve(OWNER, OWNER)
    state.business[BIZ] = {"user_id": OWNER, "user_chat_id": OWNER,
                           "is_enabled": True, "rights": dict(ALL_RIGHTS)}
    state.api = FakeBotAPI()
    yield state.api
    asyncio.run(db.close())
    state.api = None
    state.users.clear()
    state.business.clear()
    config.OWNER_ID = 0


def seen(user_id, name, username=None, *, days_ago=0, owner=OWNER):
    at = db.now() - days_ago * DAY
    asyncio.run(db.execute(
        "INSERT INTO aliases(owner_id,user_id,name,username,first_seen,last_seen)"
        " VALUES (?,?,?,?,?,?)", (owner, user_id, name, username, at, at)))


def everyone():
    return asyncio.run(namelog.everyone(OWNER))


def one(user_id=VASYA):
    return asyncio.run(namelog.one(OWNER, user_id))


# ------------------------------------------------------------- один человек -

def test_a_person_who_never_renamed_is_said_so_plainly():
    seen(VASYA, "Вася", "vasya")
    text = one()
    assert "не менялось" in text and "Вася" in text


def test_the_chain_runs_from_the_oldest_to_the_newest():
    seen(VASYA, "Вася", "vasya", days_ago=40)
    seen(VASYA, "Василий", "vasya", days_ago=20)
    seen(VASYA, "Пётр", "petya", days_ago=1)
    # Текущее имя есть и в заголовке — порядок смотрим только в самой цепочке.
    chain = one().split("раза:", 1)[1]
    assert chain.index("Вася") < chain.index("Василий") < chain.index("Пётр")


def test_the_first_name_is_an_introduction_not_a_rename():
    seen(VASYA, "Вася", "vasya", days_ago=40)
    seen(VASYA, "Пётр", "petya", days_ago=1)
    text = one()
    assert "когда я его увидел" in text
    assert "**1** раз" in text, "переименование одно, имён два"


def test_a_username_change_is_shown_too():
    seen(VASYA, "Вася", "vasya", days_ago=10)
    seen(VASYA, "Вася", "vasya_new", days_ago=1)
    assert "@vasya_new" in one() and "@vasya)" in one()


def test_an_unknown_person_does_not_crash():
    assert "не менялось" in asyncio.run(namelog.one(OWNER, 123456))


# ----------------------------------------------------------------- все -----

def test_nobody_renamed_explains_where_history_comes_from():
    seen(VASYA, "Вася", "vasya")
    text = everyone()
    assert "Никто пока не переименовывался" in text
    assert "задним числом" in text, "иначе непонятно, почему пусто"


def test_only_people_who_actually_renamed_are_listed():
    seen(VASYA, "Вася", days_ago=10)
    seen(VASYA, "Пётр", days_ago=1)
    seen(MARINA, "Марина", days_ago=5)          # одно имя — не переименование
    text = everyone()
    assert "Переименовывались: 1" in text
    assert "Марина" not in text


def test_the_freshest_rename_comes_first():
    seen(VASYA, "Вася", days_ago=40)
    seen(VASYA, "Пётр", days_ago=30)
    seen(MARINA, "Марина", days_ago=20)
    seen(MARINA, "Марина Н.", days_ago=1)
    text = everyone()
    assert text.index("Марина Н.") < text.index("Пётр")


def test_a_long_chain_is_trimmed_and_says_so():
    for i in range(12):
        seen(VASYA, f"Имя{i}", days_ago=30 - i)
    text = everyone()
    assert "и раньше ещё" in text
    assert "Имя11" in text, "свежее имя показываем всегда"
    assert "Имя0" not in text, "самое старое — за обрезом"


def test_other_owners_history_is_not_shown():
    seen(VASYA, "Чужой", days_ago=10, owner=OTHER)
    seen(VASYA, "Чужой 2", days_ago=1, owner=OTHER)
    assert "Никто пока" in everyone()


# ------------------------------------------------------------- команды -----

def test_dot_command_answers_privately():
    seen(VASYA, "Вася", days_ago=10)
    seen(VASYA, "Пётр", days_ago=1)
    api = state.api
    asyncio.run(dotcmd.handle(api, business_message(".names", chat_id=VASYA,
                                                    from_id=OWNER), BIZ, OWNER))
    assert api.all_deleted_ids == [10], "команда убрана из переписки"
    assert "Пётр" in api.texts_to(OWNER)[-1]
    assert not [t for _, t, biz in api.sent if biz], "собеседник ничего не видит"


def test_dot_command_takes_the_person_from_a_reply():
    seen(MARINA, "Марина", days_ago=10)
    seen(MARINA, "Марина Н.", days_ago=1)
    api = state.api
    message = business_message(".names", chat_id=VASYA, from_id=OWNER,
                               reply_to={"message_id": 3,
                                         "from": {"id": MARINA,
                                                  "first_name": "Марина"}})
    asyncio.run(dotcmd.handle(api, message, BIZ, OWNER))
    assert f"`{MARINA}`" in api.texts_to(OWNER)[-1]


def bot_command(text):
    asyncio.run(commands.handle(state.api, {
        "message_id": 1, "date": 1700000000,
        "chat": {"id": OWNER, "type": "private"},
        "from": {"id": OWNER, "first_name": "Я"}, "text": text}))
    return state.api


def test_slash_without_an_id_lists_everyone():
    seen(VASYA, "Вася", days_ago=10)
    seen(VASYA, "Пётр", days_ago=1)
    assert "Переименовывались" in bot_command("/names").texts[-1]


def test_slash_with_an_id_shows_one_person():
    seen(VASYA, "Вася", days_ago=10)
    seen(VASYA, "Пётр", days_ago=1)
    text = bot_command(f"/names {VASYA}").texts[-1]
    assert f"`{VASYA}`" in text and "Вася" in text
