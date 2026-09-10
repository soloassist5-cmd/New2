"""Сквозной сценарий мута на заглушках Telethon: команда -> база -> перехват."""
import asyncio

import pytest

import config
import db
from core import dispatcher, state
from modules import mute

ME = 1               # id «своего» аккаунта в заглушках
CHAT = 777           # личка: chat_id == id собеседника
PEER = 777
GROUP = -100500


class FakeEntity:
    def __init__(self, entity_id, name="Собеседник"):
        self.id, self.first_name, self.last_name, self.username = entity_id, name, None, None


class FakeClient:
    def __init__(self):
        self.deleted: list[tuple] = []

    async def get_entity(self, ref):
        return FakeEntity(int(ref) if str(ref).lstrip("-").isdigit() else 1)

    async def delete_messages(self, chat, ids, revoke=False):
        self.deleted.append((chat, tuple(ids), revoke))


class FakeReply:
    """Сообщение, на которое отвечают командой."""

    def __init__(self, sender_id, msg_id=100):
        self.sender_id, self.id, self.message = sender_id, msg_id, "чужое сообщение"

    async def get_sender(self):
        return FakeEntity(self.sender_id, "Вася")


class FakeMessage:
    def __init__(self, text="", msg_id=1):
        self.message, self.id, self.media, self.frames = text, msg_id, None, []

    async def edit(self, text, link_preview=False):
        self.frames.append(text)
        return self

    async def delete(self):
        return None


class FakeEvent:
    """Достаточно полей, чтобы отработали Ctx.resolve_target и mute._enforce."""

    def __init__(self, text="", chat_id=CHAT, is_private=True, out=True,
                 sender_id=PEER, msg_id=1, media=None, reply_to=None):
        self.message = FakeMessage(text, msg_id)
        self.message.media = media
        self.chat_id, self.is_private, self.out = chat_id, is_private, out
        self.sender_id, self.client = sender_id, state.client
        self.reply = FakeReply(reply_to) if reply_to is not None else None
        self.pattern_match = dispatcher.build_pattern().match(text)

    async def get_reply_message(self):
        return self.reply

    async def get_chat(self):
        return FakeEntity(self.chat_id)

    async def get_sender(self):
        return FakeEntity(self.sender_id)

    async def get_input_chat(self):
        return self.chat_id


@pytest.fixture(autouse=True)
def instant_sleep(monkeypatch):
    """Команды с delete_after ждут по-настоящему — в тестах это лишние секунды."""
    async def noop(_seconds):
        return None
    monkeypatch.setattr(dispatcher.asyncio, "sleep", noop)


@pytest.fixture(autouse=True)
def env():
    config.DB_PATH.unlink(missing_ok=True)
    config.ANIM_DELAY = 0
    state.client = FakeClient()
    state.me = FakeEntity(ME, "Я")
    state.log_entity = None          # без сети: send_log сразу возвращает None
    asyncio.run(db.init())
    state.mutes.clear()
    yield
    asyncio.run(db.close())
    state.client = state.me = None


def dispatch(text, **kwargs):
    """Прогоняет строку через настоящий диспетчер и отдаёт сообщение команды."""
    event = FakeEvent(text, **kwargs)
    asyncio.run(dispatcher._handle(event))
    return event.message


def test_mute_in_private_chat_edits_own_message():
    msg = dispatch(".mute")
    assert msg.frames[-1] == "🔇 **Вы были замучены на неопределённый срок.**"
    assert (ME, CHAT, PEER) in state.mutes


def test_mute_with_duration_and_reason():
    msg = dispatch(".mute 2h капслок")
    assert "на 2 часа" in msg.frames[-1]
    assert "Причина: капслок" in msg.frames[-1]
    assert state.mutes[(ME, CHAT, PEER)] > 0, "срок должен быть записан"


def test_quiet_flag_skips_animation():
    msg = dispatch(".mute -q")
    assert len(msg.frames) == 1, "с -q должна быть ровно одна правка"


def test_mute_survives_restart_via_db():
    dispatch(".mute 1d")
    state.mutes.clear()
    asyncio.run(state.load_mutes())
    assert asyncio.run(state.is_muted(ME, CHAT, PEER))


def test_unmute_restores_speech():
    dispatch(".mute")
    msg = dispatch(".unmute")
    assert msg.frames[-1] == "🔊 **С вас снят мут. Можете писать.**"
    assert not asyncio.run(state.is_muted(ME, CHAT, PEER))


def test_unmute_without_mute_reports_error():
    msg = dispatch(".unmute")
    assert "не был замучен" in msg.frames[-1]


def test_muted_message_is_deleted_for_everyone():
    dispatch(".mute")
    incoming = FakeEvent("привет", out=False, msg_id=42)
    asyncio.run(mute._enforce(incoming))
    assert state.client.deleted == [(CHAT, (42,), True)], "удаление должно быть revoke=True"
    # Своё удаление не должно попасть в журнал как «удалено собеседником».
    assert state.was_own_deletion(ME, CHAT, 42)
    assert state.was_own_deletion(ME, None, 42)


def test_unmuted_message_is_left_alone():
    incoming = FakeEvent("привет", out=False, msg_id=43)
    asyncio.run(mute._enforce(incoming))
    assert state.client.deleted == []


def test_own_messages_are_never_deleted():
    dispatch(".mute")
    own = FakeEvent("моё сообщение", out=True, msg_id=44)
    asyncio.run(mute._enforce(own))
    assert state.client.deleted == []


def test_chat_mute_does_not_leak_into_other_chats():
    dispatch(".mute")
    elsewhere = FakeEvent("привет", chat_id=GROUP, is_private=False, out=False,
                          sender_id=PEER, msg_id=45)
    asyncio.run(mute._enforce(elsewhere))
    assert state.client.deleted == []


def test_gmute_applies_everywhere():
    dispatch(".gmute")
    elsewhere = FakeEvent("привет", chat_id=GROUP, is_private=False, out=False,
                          sender_id=PEER, msg_id=46)
    asyncio.run(mute._enforce(elsewhere))
    assert state.client.deleted == [(GROUP, (46,), True)]


def test_expired_mute_stops_deleting():
    dispatch(".mute 1s")
    state.mutes[(ME, CHAT, PEER)] = db.now() - 1    # промотали время вперёд
    incoming = FakeEvent("привет", out=False, msg_id=47)
    asyncio.run(mute._enforce(incoming))
    assert state.client.deleted == []


def test_mute_in_group_requires_a_target():
    """В группе собеседник не определяется сам по себе — нужен реплай или @user."""
    msg = dispatch(".mute 10m", chat_id=GROUP, is_private=False)
    assert "Кого мутим?" in msg.frames[-1]
    assert state.mutes == {}


def test_mute_by_reply_in_group_names_the_target():
    """Сообщение в группе публичное — оно должно называть, кого замутили."""
    msg = dispatch(".mute 10m", chat_id=GROUP, is_private=False, reply_to=555)
    assert "Вы были замучены" not in msg.frames[-1]
    assert "замучен(а) на 10 минут" in msg.frames[-1]
    assert (ME, GROUP, 555) in state.mutes


def test_self_mute_is_refused():
    msg = dispatch(".mute", reply_to=ME)
    assert "Себя мутить бессмысленно" in msg.frames[-1]


def test_unknown_command_is_ignored():
    msg = dispatch(".такойкомандынет")
    assert msg.frames == []
