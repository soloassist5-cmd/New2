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


# ------------------------------------------------------- история имён ------

def alias(name, username=None, user=VASYA):
    db.forget_aliases()                    # иначе кэш процесса съест изменение
    return asyncio.run(db.note_alias(OWNER, user, name, username))


def test_a_name_is_recorded_once_not_on_every_message():
    assert alias("Вася", "vasya") is False, "первое имя — не переименование"
    assert alias("Вася", "vasya") is False
    rows = asyncio.run(db.aliases(OWNER, VASYA))
    assert len(rows) == 1


def test_a_rename_adds_a_row_and_closes_the_previous_one():
    alias("Вася", "vasya")
    assert alias("Пётр", "petya") is True
    rows = asyncio.run(db.aliases(OWNER, VASYA))
    assert [row["name"] for row in rows] == ["Пётр", "Вася"]
    assert rows[1]["last_seen"] >= rows[1]["first_seen"]


def test_a_username_change_alone_counts_as_a_rename():
    alias("Вася", "vasya")
    assert alias("Вася", "vasya2") is True


def test_card_lists_previous_names_but_not_the_current_one():
    cached()
    alias("Вася", "vasya")
    alias("Василий П.", "vasya")
    alias("Вася Пупкин", "vasya_new")
    text = card(user=PROFILE)
    assert "Раньше звали иначе" in text
    assert "Василий П." in text and "@vasya)" in text
    assert text.count("Вася Пупкин") == 1, "текущее имя — только в шапке"


def test_the_short_name_fallback_steps_aside_for_real_history():
    """Две версии «раньше звали» в одной карточке — это каша."""
    cached()
    alias("Вася", "vasya")
    alias("Пётр Иванов", "petya")
    text = card(user=dict(PROFILE, first_name="Пётр", last_name="Иванов"))
    assert "раньше был(а)" not in text
    assert "Раньше звали иначе" in text


def test_alias_is_recorded_from_a_real_message():
    from bot import business

    api = state.api
    asyncio.run(business.on_business_message(api, business_message(
        "привет", chat_id=VASYA, from_id=VASYA, first_name="Вася")))
    rows = asyncio.run(db.aliases(OWNER, VASYA))
    assert [row["name"] for row in rows] == ["Вася"]


def test_the_owners_own_messages_do_not_build_a_history_on_themselves():
    from bot import business

    api = state.api
    asyncio.run(business.on_business_message(api, business_message(
        "это я", chat_id=VASYA, from_id=OWNER, first_name="Я")))
    assert asyncio.run(db.aliases(OWNER, OWNER)) == []


# ---------------------------------------------------------- как пишет ------

def at_hour(hour, day=0, minute=0):
    base = 1_757_600_000 - (1_757_600_000 % 86400)
    return base + day * 86400 + hour * 3600 + minute * 60


def wrote(stamps, user=VASYA):
    for i, stamp in enumerate(stamps):
        asyncio.run(db.cache_message(OWNER, user, i, user, True, f"т{i}",
                                     None, None, None, stamp, user_name="Вася"))


def test_a_handful_of_messages_is_not_a_habit():
    wrote([at_hour(23, day=d) for d in range(5)])
    assert "Как пишет" not in card()


def test_the_usual_hours_are_reported():
    wrote([at_hour(23, day=d, minute=m)
           for d in range(10) for m in (0, 20, 40)])
    text = card()
    assert "Как пишет" in text and "чаще пишет с" in text


def test_a_night_writer_is_flagged():
    wrote([at_hour(2, day=d, minute=m) for d in range(10) for m in (0, 30, 50)])
    assert "ночью" in card()


def test_a_daytime_writer_is_not_called_nocturnal():
    wrote([at_hour(14, day=d, minute=m) for d in range(10) for m in (0, 30, 50)])
    assert "ночью" not in card()


def test_a_long_silence_is_noticed():
    stamps = [at_hour(12, day=d) for d in range(10)]
    stamps += [at_hour(12, day=d) for d in range(30, 40)]
    wrote(stamps)
    assert "молчал(а)" in card()


def test_a_chatty_day_shows_an_average():
    wrote([at_hour(12, day=d, minute=m)
           for d in range(10) for m in (0, 10, 20)])
    assert "сообщений в день" in card()


# ----------------------------------------------------- дата регистрации ----

def test_the_card_dates_the_account_from_its_number():
    cached()
    assert "заведён примерно" in card(user=PROFILE)


def test_an_unknown_person_still_gets_the_account_age():
    text = card(350_000_000)
    assert "ничего нет" in text
    assert "Судя по номеру" in text and "2017" in text
