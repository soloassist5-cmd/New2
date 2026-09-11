"""Досье на собеседника: только своя база, ничего наружу, ничего собеседнику."""
import asyncio

import pytest

import config
import db
from bot import commands, dossier, dotcmd
from core import state
from tests.fakes import ALL_RIGHTS, FakeBotAPI, approve, business_message

OWNER, VASYA, OTHER = 111, 777, 222
BIZ = "biz1"
NOW = 1_757_600_000

PROFILE = {"id": VASYA, "first_name": "Вася", "last_name": "Пупкин",
           "username": "vasya", "is_premium": True, "language_code": "ru"}


@pytest.fixture(autouse=True)
def env():
    from core import chatprefs

    config.DB_PATH.unlink(missing_ok=True)
    asyncio.run(db.init())
    chatprefs.invalidate_all()
    state.users.clear()
    state.business.clear()
    state.mutes.clear()
    state.allowlist.clear()
    state.forget_own_deletions()
    config.OWNER_ID = OWNER
    approve(OWNER, OWNER)
    state.business[BIZ] = {"user_id": OWNER, "user_chat_id": OWNER,
                           "is_enabled": True, "rights": dict(ALL_RIGHTS)}
    state.api = FakeBotAPI()
    state.api.chat = None                  # getChat по умолчанию не отвечает
    yield state.api
    asyncio.run(db.close())
    state.api = None
    state.users.clear()
    state.business.clear()
    state.mutes.clear()


def cached(n=1, *, owner=OWNER, user=VASYA, media=None):
    for i in range(n):
        asyncio.run(db.cache_message(owner, user, i, user, True, f"текст {i}",
                                     media, None, None, NOW + i,
                                     user_name="Вася"))


def card(user_id=VASYA, *, chat_id=None, user=None) -> str:
    return asyncio.run(dossier.card(state.api, OWNER, user_id,
                                    chat_id=chat_id, user=user))


# ------------------------------------------------------------- содержимое --

def test_unknown_person_says_so_instead_of_an_empty_card():
    text = card(555)
    assert "`555`" in text and "ничего нет" in text


def test_card_counts_what_the_bot_stored():
    cached(5, media="фото")
    asyncio.run(db.add_deleted({"owner_id": OWNER, "chat_id": VASYA, "msg_id": 1,
                                "user_id": VASYA, "user_name": "Вася",
                                "text": "x", "date": NOW}))
    asyncio.run(db.add_edit(OWNER, VASYA, 1, VASYA, "было", "стало"))
    text = card()
    assert "сохранено сообщений: **5**" in text and "вложений 5" in text
    assert "удалял(а) у всех: **1**" in text
    assert "правил(а) сообщения: **1**" in text


def test_intercepted_is_split_by_reason():
    for reason in ("mute", "mute", "dnd"):
        asyncio.run(db.add_intercepted(
            {"owner_id": OWNER, "chat_id": VASYA, "msg_id": 1, "user_id": VASYA,
             "user_name": "Вася", "text": "y", "date": NOW}, reason))
    text = card()
    assert "перехвачено: **3**" in text
    assert "мут 2" in text and "не беспокоить 1" in text


def test_active_mute_and_allowlist_show_up():
    cached()
    asyncio.run(db.add_mute(OWNER, 0, VASYA, 0, "достал"))
    asyncio.run(db.allow(OWNER, VASYA, "Вася"))
    text = card()
    assert "замучен(а) во всех чатах, бессрочно · достал" in text
    assert "белом списке" in text


def test_expired_mute_is_not_reported_as_active():
    cached()
    asyncio.run(db.add_mute(OWNER, VASYA, VASYA, db.now() - 60, None))
    assert "замучен" not in card()


def test_urgent_calls_are_counted():
    cached()
    asyncio.run(db.add_urgent_call(OWNER, VASYA, "Вася", VASYA, "перезвони"))
    assert "срочных вызовов: **1**" in card()


def test_ignored_chat_is_flagged():
    cached()
    asyncio.run(db.set_setting(OWNER, VASYA, "ignored", 1))
    assert "в игноре" in card(chat_id=VASYA)


# ------------------------------------------------------------------ имена --

def test_live_profile_beats_the_stored_name():
    cached()
    text = card(user=PROFILE)
    assert "Вася Пупкин" in text and "@vasya" in text
    assert "Premium" in text and "🌐 ru" in text


def test_a_shortened_stored_name_is_not_a_rename():
    """В базе лежит «Вася», в сообщении «Вася Пупкин» — это один человек."""
    cached()
    assert "раньше" not in card(user=PROFILE)


def test_a_real_rename_is_shown():
    cached()
    renamed = dict(PROFILE, first_name="Пётр", last_name="Иванов")
    assert "раньше был(а): Вася" in card(user=renamed)


def test_bio_comes_from_get_chat():
    cached()
    state.api.chat = {"id": VASYA, "first_name": "Вася", "bio": "не пишу первым",
                      "birthdate": {"day": 3, "month": 7}}
    text = card()
    assert "не пишу первым" in text and "03.07" in text


def test_a_silent_get_chat_does_not_break_the_card():
    cached()
    state.api.chat = None                  # заглушка бросит ошибку
    assert "сохранено сообщений: **1**" in card()


# ---------------------------------------------------------------- чужое ----

def test_other_owners_data_is_not_counted():
    cached(3, owner=OTHER)
    assert "ничего нет" in card()


# --------------------------------------------------------------- команды ---

def test_dot_command_answers_privately_and_leaves_no_trace():
    cached(2)
    api = state.api
    asyncio.run(dotcmd.handle(api, business_message(".dox", chat_id=VASYA,
                                                    from_id=OWNER), BIZ, OWNER))
    assert api.all_deleted_ids == [10], "команда убрана из переписки"
    assert api.texts_to(OWNER), "досье приходит в личку с ботом"
    assert not [text for chat, text, biz in api.sent if biz], \
        "собеседник не должен увидеть ни строчки"
    assert "сохранено сообщений: **2**" in api.texts_to(OWNER)[-1]


def test_dot_command_takes_the_person_from_a_reply():
    cached(1, user=OTHER)
    api = state.api
    message = business_message(".dox", chat_id=VASYA, from_id=OWNER,
                               reply_to={"message_id": 3,
                                         "from": {"id": OTHER,
                                                  "first_name": "Другой"}})
    asyncio.run(dotcmd.handle(api, message, BIZ, OWNER))
    assert f"`{OTHER}`" in api.texts_to(OWNER)[-1]


def test_slash_command_needs_an_id():
    api = state.api
    asyncio.run(commands.handle(api, {
        "message_id": 1, "date": NOW, "chat": {"id": OWNER, "type": "private"},
        "from": {"id": OWNER, "first_name": "Я"}, "text": "/dox"}))
    assert "Использование" in api.texts[-1]


def test_slash_command_reports_by_id():
    cached(4)
    api = state.api
    asyncio.run(commands.handle(api, {
        "message_id": 1, "date": NOW, "chat": {"id": OWNER, "type": "private"},
        "from": {"id": OWNER, "first_name": "Я"}, "text": f"/dox {VASYA}"}))
    assert "сохранено сообщений: **4**" in api.texts[-1]
