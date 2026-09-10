"""Маршрутизация отчётов: бот из @BotFather или запасной лог-чат."""
import asyncio
import io

import pytest
from telethon.tl.types import InputPeerUser

from core import reporter, state

OWNER = 555
CARD = "🗑 **Удалённое сообщение**\nтекст"


class FakeUserbot:
    """Юзербот: пишет в лог-чат и достаёт копии медиа из хранилища."""

    def __init__(self, staged=b"payload"):
        self.sent: list[tuple] = []
        self.staged = staged

    async def send_message(self, entity, text, link_preview=False, reply_to=None):
        self.sent.append((entity, text, reply_to))
        return object()

    async def get_messages(self, entity, ids=None):
        if self.staged is None:
            return None
        message = type("Staged", (), {})()
        message.media = object()
        message.file = type("F", (), {"name": "photo.jpg"})()
        return message

    async def download_media(self, message, file=None):
        file.write(self.staged)
        return file


class FakeBot:
    def __init__(self, fail=False, known=True):
        self.messages: list[str] = []
        self.files: list[tuple] = []
        self.fail, self.known = fail, known

    async def get_input_entity(self, user_id):
        if not self.known:
            raise ValueError("не видел этого пользователя")
        return InputPeerUser(user_id, 12345)

    async def send_message(self, peer, text, link_preview=False):
        if self.fail:
            raise RuntimeError("bot can't initiate conversation")
        self.messages.append(text)

    async def send_file(self, peer, payload, caption=None):
        if self.fail:
            raise RuntimeError("bot can't initiate conversation")
        self.files.append((payload.read() if isinstance(payload, io.BytesIO) else payload,
                           caption))


@pytest.fixture(autouse=True)
def env():
    state.client = FakeUserbot()
    state.log_entity = "log"
    state.bot = None
    state.owner_id = OWNER
    state.owner_peer = None
    state.bot_blocked = False
    yield
    state.client = state.bot = state.owner_peer = state.log_entity = None


def use_bot(**kwargs):
    state.bot = FakeBot(**kwargs)
    asyncio.run(reporter.resolve_owner())
    return state.bot


def test_without_bot_report_goes_to_log_chat():
    assert asyncio.run(reporter.send_report(CARD)) is True
    assert state.client.sent[0][0] == "log"
    assert state.client.sent[0][1] == CARD


def test_with_bot_report_goes_to_owner_only():
    bot = use_bot()
    assert asyncio.run(reporter.send_report(CARD)) is True
    assert bot.messages == [CARD]
    assert state.client.sent == [], "в лог-чат дублировать не нужно"


def test_resolve_owner_falls_back_to_bare_peer():
    bot = use_bot(known=False)
    assert isinstance(state.owner_peer, InputPeerUser)
    assert state.owner_peer.user_id == OWNER
    assert bot.known is False


def test_media_is_attached_to_the_card():
    bot = use_bot()
    asyncio.run(reporter.send_report(CARD, media_ref=7))
    assert bot.files and bot.files[0][0] == b"payload"
    assert bot.files[0][1] == CARD, "короткий текст уходит подписью к медиа"
    assert bot.messages == []


def test_long_card_is_split_from_media():
    bot = use_bot()
    long_card = "х" * (reporter.CAPTION_LIMIT + 10)
    asyncio.run(reporter.send_report(long_card, media_ref=7))
    assert bot.messages == [long_card], "длинный текст не влезает в подпись"
    assert bot.files and bot.files[0][1] is None


def test_missing_staged_media_still_delivers_text():
    state.client = FakeUserbot(staged=None)
    bot = use_bot()
    asyncio.run(reporter.send_report(CARD, media_ref=7))
    assert bot.messages == [CARD] and bot.files == []


def test_blocked_bot_falls_back_and_warns_once():
    use_bot(fail=True)
    assert asyncio.run(reporter.send_report(CARD)) is True
    texts = [text for _, text, _ in state.client.sent]
    assert reporter.NO_START_HINT in texts, "владельцу подсказываем нажать Start"
    assert CARD in texts, "сам отчёт теряться не должен"

    asyncio.run(reporter.send_report("второй"))
    assert texts.count(reporter.NO_START_HINT) == 1, "подсказка не должна повторяться"


def test_recovered_bot_can_warn_again_later():
    bot = use_bot(fail=True)
    asyncio.run(reporter.send_report(CARD))
    assert state.bot_blocked is True
    bot.fail = False
    asyncio.run(reporter.send_report("снова"))
    assert state.bot_blocked is False, "связь восстановилась — флаг снимается"
