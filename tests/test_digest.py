"""Сводка перехваченного: разбивка по чатам, списком и файлом.

Раньше строки шли сплошным потоком по времени — на двух собеседниках это уже
каша, а глазами нужен ответ на вопрос «что мне писал вот этот».
"""
import asyncio

import pytest

import config
import db
from bot import digest, transcript
from core import state
from tests.fakes import FakeBotAPI, approve

OWNER, VASYA, MARINA = 111, 777, 888
BASE = 1_757_600_000


@pytest.fixture(autouse=True)
def env():
    config.DB_PATH.unlink(missing_ok=True)
    asyncio.run(db.init())
    config.OWNER_ID = OWNER
    state.users.clear()
    approve(OWNER, OWNER)
    state.api = FakeBotAPI()
    yield state.api
    asyncio.run(db.close())
    state.users.clear()
    state.api = None


def put(chat_id, name, text, *, at, reason="mute", media=None):
    asyncio.run(db.add_intercepted({
        "owner_id": OWNER, "chat_id": chat_id, "msg_id": at, "user_id": chat_id,
        "user_name": name, "text": text, "media_type": media,
        "file_id": "F" if media else None, "date": BASE + at,
    }, reason))


def rows():
    return asyncio.run(db.intercepted(OWNER, limit=500))


def deliver(title="🔇 **Перехваченное**"):
    asyncio.run(digest.deliver(OWNER, rows(), title=title, empty="пусто"))
    return state.api


def two_chats(per_chat=3):
    for i in range(per_chat):
        put(VASYA, "Вася", f"вася {i}", at=100 + i)
        put(MARINA, "Марина", f"марина {i}", at=200 + i)


# ------------------------------------------------------------- группировка --

def test_group_keeps_each_chat_together_and_sorted():
    two_chats()
    groups = digest.group(rows())
    assert [chat for chat, _ in groups] == [MARINA, VASYA], "свежий чат — первым"
    for _, messages in groups:
        dates = [row["date"] for row in messages]
        assert dates == sorted(dates), "внутри чата — по времени"


def test_group_survives_rows_without_a_chat():
    put(VASYA, "Вася", "есть чат", at=1)
    asyncio.run(db.add_intercepted(
        {"owner_id": OWNER, "chat_id": None, "user_id": 5, "user_name": "Ноль",
         "text": "нет чата", "date": BASE + 2}, "mute"))
    assert len(digest.group(rows())) == 2


# ------------------------------------------------------------------ список --

def test_short_list_is_split_by_chat():
    two_chats(2)
    api = deliver()
    text = api.texts_to(OWNER)[-1]
    assert f"`{VASYA}`" in text and f"`{MARINA}`" in text, "id чата виден"
    assert text.count("💬") == 2, "по заголовку на чат"
    # Сообщения одного чата идут подряд, а не вперемешку по времени.
    order = [line for line in text.splitlines() if "вася" in line or "марина" in line]
    assert order == ["  марина 0", "  марина 1", "  вася 0", "  вася 1"]


def test_sender_name_is_dropped_when_the_chat_has_only_one():
    """В личке имя на каждой строке — это одно и то же слово шесть раз."""
    two_chats(3)
    text = deliver().texts_to(OWNER)[-1]
    assert text.count("Вася") == 1, "имя только в заголовке чата"


def test_sender_name_stays_when_the_chat_has_several():
    """В группе отправители разные — без имени строка бесполезна."""
    put(VASYA, "Вася", "раз", at=1)
    put(VASYA, "Петя", "два", at=2)
    lines = deliver().texts_to(OWNER)[-1].splitlines()
    signed = [line for line in lines if line.startswith("• ")]
    assert len(signed) == 2
    assert all("**" in line for line in signed), "у каждой строки свой отправитель"


def test_reason_marker_only_where_reasons_differ():
    put(VASYA, "Вася", "мут", at=1, reason="mute")
    put(MARINA, "Марина", "не беспокоить", at=2, reason="dnd")
    only_one_kind = deliver().texts_to(OWNER)[-1]
    assert "🔇" not in only_one_kind.replace("🔇 **Перехваченное**", ""), \
        "в каждом чате причина одна — помечать нечего"

    put(VASYA, "Вася", "и так тоже", at=3, reason="dnd")
    mixed = deliver().texts_to(OWNER)[-1]
    assert "🌙" in mixed, "в чате две причины — показываем, какая где"


def test_empty_says_so_and_reports_nothing():
    api = deliver()
    assert api.texts_to(OWNER) == ["пусто"]


# -------------------------------------------------------------------- файл --

def test_long_report_goes_as_a_file_split_by_chat():
    two_chats(digest.LIST_LIMIT)
    api = deliver()
    assert api.files, "длинную сводку отдаём файлом"
    text = api.uploads[-1].decode()

    assert text.count("chat_id:") == 2
    assert f"chat_id: {VASYA}" in text and f"chat_id: {MARINA}" in text
    assert "Чатов: 2" in text
    assert f"Всего сообщений: {digest.LIST_LIMIT * 2}" in text

    # Каждый чат — цельным куском: между его первым и последним сообщением
    # не должно быть чужих.
    vasya = text.index(f"chat_id: {VASYA}")
    after = text[vasya:]
    assert "марина" not in after[:after.index("chat_id:") if "chat_id:" in after
                                 else len(after)]


def test_file_caption_counts_chats_and_media():
    two_chats(digest.LIST_LIMIT)
    put(VASYA, "Вася", "", at=999, media="фото")
    api = deliver()
    caption = api.captions[-1]
    assert "чатов: **2**" in caption
    assert "вложений: **1**" in caption


def test_file_header_lists_reasons():
    two_chats(digest.LIST_LIMIT)
    put(MARINA, "Марина", "тишина", at=900, reason="dnd")
    text = deliver().uploads[-1].decode()
    assert "Причина «мут»" in text and "Причина «не беспокоить»" in text


def test_chat_section_carries_its_own_totals():
    two_chats(digest.LIST_LIMIT)
    put(VASYA, "Вася", "", at=901, media="голосовое")
    text = deliver().uploads[-1].decode()
    section = text[text.index(f"chat_id: {VASYA}"):]
    assert f"сообщений: {digest.LIST_LIMIT + 1}" in section
    assert "вложений: 1" in section
    assert "период:" in section


def test_grouped_file_of_nothing_does_not_crash():
    payload, stats = transcript.build_grouped([], owner_id=OWNER, when=BASE,
                                              titles={})
    assert stats == {"total": 0, "chats": 0, "media": 0, "first": None,
                     "last": None}
    assert "Пусто" in payload.decode()


# ------------------------------------------------------------- порог -------

def test_the_threshold_decides_list_or_file():
    """Ровно на пороге — ещё список, на единицу больше — уже файл."""
    two_chats(digest.LIST_LIMIT // 2)
    api = deliver()
    assert not api.files and api.texts_to(OWNER)

    put(VASYA, "Вася", "лишнее", at=777)
    api = deliver()
    assert api.files, "перевалили за порог — должен быть файл"


def test_zero_threshold_always_sends_a_file(monkeypatch):
    """Кому список не нужен вовсе — DIGEST_LIST_LIMIT=0."""
    monkeypatch.setattr(digest, "LIST_LIMIT", 0)
    put(VASYA, "Вася", "одно-единственное", at=1)
    api = deliver()
    assert api.files, "при нулевом пороге даже одно сообщение уходит файлом"


def test_the_threshold_comes_from_config():
    assert digest.LIST_LIMIT == config.DIGEST_LIST_LIMIT
