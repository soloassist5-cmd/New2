"""Команды «.» внутри личных чатов в режиме Business."""
import asyncio

import pytest

import config
import db
from bot import dotcmd
from core import state
from tests.fakes import ALL_RIGHTS, FakeBotAPI, business_message

OWNER = 111
OWNER_CHAT = 111
PEER = 777
BIZ = "biz1"
MUTED_FOREVER = "🔇 **Вы были замучены на неопределённый срок.**"


@pytest.fixture(autouse=True)
def env():
    from modules.antidelete import invalidate_all

    config.DB_PATH.unlink(missing_ok=True)
    asyncio.run(db.init())
    invalidate_all()
    config.ANIM_DELAY = 0
    state.mutes.clear()
    state.forget_own_deletions()
    state.owner_id, state.owner_chat_id = OWNER, OWNER_CHAT
    state.business.clear()
    state.business[BIZ] = {"user_id": OWNER, "user_chat_id": OWNER_CHAT,
                           "is_enabled": True, "rights": dict(ALL_RIGHTS)}
    state.api = FakeBotAPI()
    yield
    asyncio.run(db.close())
    state.api = None
    state.business.clear()


def run(text, *, api=None, **kwargs):
    api = api or state.api
    message = business_message(text, from_id=OWNER, **kwargs)
    asyncio.run(dotcmd.handle(api, message, BIZ))
    return api


# ------------------------------------------------------------------ мут ----

def test_mute_replaces_command_with_an_animated_notice():
    api = run(".mute", message_id=10)
    assert api.deleted == [(BIZ, [10])], "команда убрана из переписки"

    posted = [(chat, text) for chat, text, biz in api.sent if biz == BIZ]
    assert posted and posted[0][0] == PEER, "уведомление ушло в саму переписку"
    assert posted[0][1].startswith("🔇 ▱"), "первый кадр — пустая полоса"
    assert api.edits[-1][2] == MUTED_FOREVER
    assert (PEER, PEER) in state.mutes


def test_intermediate_frames_have_no_markdown():
    api = run(".mute", message_id=10)
    for _, _, text in api.edits[:-1]:
        assert "**" not in text


def test_mute_with_duration_and_reason():
    api = run(".mute 2h капслок", message_id=11)
    final = api.edits[-1][2]
    assert "на 2 часа" in final and "Причина: капслок" in final
    assert state.mutes[(PEER, PEER)] > 0


def test_quiet_flag_skips_animation():
    api = run(".mute -q", message_id=12)
    assert len(api.edits) == 1


def test_gmute_marks_all_chats():
    run(".gmute", message_id=13)
    assert (0, PEER) in state.mutes


def test_mute_needs_the_delete_right():
    state.business[BIZ]["rights"] = {"can_read_messages": True, "can_reply": True}
    api = run(".mute", message_id=14)
    assert any("нет права удалять" in text for text in api.texts_to(OWNER_CHAT))
    assert state.mutes == {}, "без права мут бессмысленен — не сохраняем"


def test_self_mute_is_refused():
    reply = {"message_id": 5, "from": {"id": OWNER, "first_name": "Я"}}
    api = run(".mute", message_id=15, reply_to=reply)
    assert any("Себя мутить" in text for text in api.texts_to(OWNER_CHAT))


def test_unmute_announces_in_the_chat():
    run(".mute", message_id=16)
    api = run(".unmute", message_id=17)
    assert api.edits[-1][2] == "🔊 **С вас снят мут. Можете писать.**"
    assert not asyncio.run(state.is_muted(PEER, PEER))


def test_unmute_without_mute_reports_privately():
    api = run(".unmute", message_id=18)
    assert any("не был замучен" in text for text in api.texts_to(OWNER_CHAT))


def test_mute_survives_restart():
    run(".mute 1d", message_id=19)
    state.mutes.clear()
    asyncio.run(state.load_mutes())
    assert asyncio.run(state.is_muted(PEER, PEER))


# --------------------------------------------------------------- чистка ----

def test_del_removes_both_messages():
    reply = {"message_id": 30, "from": {"id": PEER, "first_name": "Он"}}
    api = run(".del", message_id=31, reply_to=reply)
    assert api.all_deleted_ids == [30, 31]
    assert state.was_own_deletion(PEER, 30)


def test_del_without_reply_explains():
    api = run(".del", message_id=32)
    assert any("Ответьте на сообщение" in text for text in api.texts_to(OWNER_CHAT))


def test_purge_covers_the_whole_range():
    reply = {"message_id": 40, "from": {"id": PEER, "first_name": "Он"}}
    api = run(".purge", message_id=45, reply_to=reply)
    assert api.all_deleted_ids == list(range(40, 46))
    assert any("Удалено сообщений: **6**" in text for text in api.texts_to(OWNER_CHAT))


# ------------------------------------------------------- приватные ответы ---

def test_informational_answers_go_to_the_bot_chat_only():
    api = run(".stats", message_id=50)
    assert api.texts_to(OWNER_CHAT), "ответ пришёл владельцу"
    assert not [t for c, t, biz in api.sent if biz == BIZ], "в переписке ничего нет"
    assert api.all_deleted_ids == [50], "сама команда убрана"


def test_deleted_list_is_private():
    api = run(".deleted", message_id=51)
    assert any("удалённых сообщений" in text.lower()
               for text in api.texts_to(OWNER_CHAT))


def test_id_reports_chat_and_sender():
    reply = {"message_id": 60, "from": {"id": PEER, "first_name": "Он"}}
    api = run(".id", message_id=61, reply_to=reply)
    answer = api.texts_to(OWNER_CHAT)[0]
    assert str(PEER) in answer and "60" in answer


def test_antidelete_toggle_is_saved():
    run(".antidelete off", message_id=70)
    from modules.antidelete import flags
    assert (asyncio.run(flags(PEER, True)))["antidelete"] is False


def test_help_marks_visible_and_private_commands():
    api = run(".help", message_id=80)
    text = api.texts_to(OWNER_CHAT)[0]
    assert "💬" in text and "🔒" in text and ".mute" in text


def test_unknown_command_is_ignored():
    api = run(".такойнет", message_id=90)
    assert api.sent == [] and api.deleted == []


def test_handler_error_is_reported_not_raised():
    @dotcmd.bizcmd("boom", desc="тест")
    async def _boom(_ctx):
        raise ValueError("сломалось")

    try:
        api = run(".boom", message_id=91)
        assert any("ValueError" in text for text in api.texts_to(OWNER_CHAT))
    finally:
        dotcmd.REGISTRY.pop("boom", None)
        dotcmd.COMMANDS[:] = [c for c in dotcmd.COMMANDS if c.name != "boom"]
