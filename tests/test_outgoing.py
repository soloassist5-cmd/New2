"""Что бот отправил за владельца — должно лежать в кэше.

Свои сообщения бот обратно апдейтом не получает, поэтому в кэш они попадают
только здесь. Если этого не сделать, удаление автоответчика, стикера или
уведомления о муте даёт владельцу карточку «в памяти ничего нет» на ровном
месте — ровно этот баг тесты ниже и стерегут.
"""
import asyncio
import pathlib
import re

import pytest

import config
import db
from bot import business, dotcmd, funcmd
from core import state
from tests.fakes import ALL_RIGHTS, FakeBotAPI, approve, business_message

OWNER, PEER = 111, 777
BIZ = "biz1"
BOT_DIR = pathlib.Path(__file__).resolve().parent.parent / "bot"


@pytest.fixture(autouse=True)
def env():
    from core import chatprefs

    config.DB_PATH.unlink(missing_ok=True)
    asyncio.run(db.init())
    chatprefs.invalidate_all()
    config.OWNER_ID = OWNER
    config.PURGE_DEBOUNCE_SEC = 0
    config.ANIM_DELAY = 0
    funcmd.SPEED = 0
    state.users.clear()
    state.business.clear()
    state.mutes.clear()
    state.forget_own_deletions()
    approve(OWNER, OWNER)
    state.business[BIZ] = {"user_id": OWNER, "user_chat_id": OWNER,
                           "is_enabled": True, "rights": dict(ALL_RIGHTS)}
    state.api = FakeBotAPI()
    yield state.api
    asyncio.run(db.close())
    funcmd.SPEED = 1.0
    state.api = None
    state.users.clear()
    state.business.clear()
    state.mutes.clear()


def incoming(text, **kwargs):
    asyncio.run(business.on_business_message(
        state.api, business_message(text, chat_id=PEER, from_id=PEER, **kwargs)))


def command(text, message_id=10):
    asyncio.run(dotcmd.handle(state.api, business_message(
        text, message_id=message_id, chat_id=PEER, from_id=OWNER), BIZ, OWNER))


def cached_ids() -> set[int]:
    rows = asyncio.run(db.fetchall(
        "SELECT msg_id FROM messages WHERE owner_id=? AND chat_id=?", (OWNER, PEER)))
    return {row["msg_id"] for row in rows}


def delete(*ids):
    asyncio.run(business.on_deleted_business_messages(state.api, {
        "business_connection_id": BIZ,
        "chat": {"id": PEER, "type": "private", "first_name": "Собеседник"},
        "message_ids": list(ids)}))
    asyncio.run(business.flush_pending())


def reports(api) -> str:
    return "\n".join(api.texts_to(OWNER))


# ------------------------------------------------------- сам баг -----------

def test_deleting_the_auto_reply_does_not_give_an_empty_card():
    """Автоответчик отправил бот — и он же обязан его помнить."""
    asyncio.run(state.set_dnd(OWNER))
    incoming("эй")
    api = state.api
    reply_id = max(cached_ids())
    api.sent.clear()

    delete(reply_id)
    assert "нет в моей памяти" not in reports(api)
    assert "Не беспокоить" in reports(api), "в карточке — текст автоответчика"


def test_deleting_a_sticker_does_not_give_an_empty_card():
    command(".rose")
    api = state.api
    assert cached_ids(), "стикер должен попасть в кэш"
    api.sent.clear()

    delete(max(cached_ids()))
    assert "нет в моей памяти" not in reports(api)


def test_deleting_a_dice_does_not_give_an_empty_card():
    command(".dice")
    api = state.api
    api.sent.clear()
    delete(max(cached_ids()))
    assert "нет в моей памяти" not in reports(api)


def test_deleting_the_mute_notice_does_not_give_an_empty_card():
    command(".mute")
    api = state.api
    api.sent.clear()
    delete(max(cached_ids()))
    assert "нет в моей памяти" not in reports(api)


def test_the_animated_notice_is_remembered_as_its_final_text():
    """Анимация переписывает сообщение — в памяти должен остаться итог."""
    command(".mute")
    rows = asyncio.run(db.get_messages(OWNER, PEER, [max(cached_ids())]))
    assert "замучен" in (rows[0]["text"] or "").lower()


def test_our_own_message_is_signed_as_ours():
    asyncio.run(state.set_dnd(OWNER))
    incoming("эй")
    api = state.api
    api.sent.clear()
    delete(max(cached_ids()))
    assert "Вы (" in reports(api), "«неизвестно» рядом со своим же id — поломка"


def test_a_genuinely_unknown_deletion_still_says_so():
    """Глушить карточку целиком нельзя: старое сообщение — законный случай."""
    api = state.api
    delete(999_999)
    assert "нет в моей памяти" in reports(api)


# ------------------------------------------------------- сторожевой --------

SENDERS = re.compile(r"\.(send_message|send_sticker|upload_sticker|send_dice)\(")
ALLOWED = {"outgoing.py", "api.py"}


def test_nothing_sends_into_a_conversation_behind_outgoings_back():
    """Новое место отправки не должно тихо появиться мимо кэша."""
    offenders = []
    for path in sorted(BOT_DIR.glob("*.py")):
        if path.name in ALLOWED:
            continue
        source = path.read_text(encoding="utf-8")
        for match in SENDERS.finditer(source):
            tail = source[match.start():match.start() + 400]
            if "business_connection_id" in tail.split(")\n")[0]:
                line = source[:match.start()].count("\n") + 1
                offenders.append(f"{path.name}:{line}")
    assert not offenders, (
        "отправка в переписку мимо bot/outgoing.py — такое сообщение не попадёт "
        f"в кэш, и его удаление даст пустую карточку: {offenders}")
