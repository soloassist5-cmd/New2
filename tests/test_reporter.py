"""Маршрутизация отчётов: бот из @BotFather или запасной лог-чат юзербота."""
import asyncio

import pytest

from core import reporter, state
from tests.fakes import approve

OWNER = 555
OWNER_CHAT = 555
CARD = "🗑 **Удалённое сообщение**\nтекст"


class FakeUserbot:
    """Юзербот: запасной путь доставки в LOG_CHAT."""

    def __init__(self):
        self.sent: list[tuple] = []

    async def send_message(self, entity, text, link_preview=False, reply_to=None):
        self.sent.append((entity, text, reply_to))
        return object()


class FakeAPI:
    def __init__(self, fail_text=False, fail_media=False):
        self.messages: list[str] = []
        self.media: list[tuple] = []
        self.fail_text, self.fail_media = fail_text, fail_media

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail_text:
            raise RuntimeError("Forbidden: bot can't initiate conversation")
        self.messages.append(text)
        return {"message_id": 1}

    async def send_media(self, chat_id, file_id, media_type, caption=None, **kwargs):
        if self.fail_media:
            raise RuntimeError("wrong file identifier")
        self.media.append((file_id, media_type, caption))
        return {"message_id": 2}


@pytest.fixture(autouse=True)
def env():
    state.client = FakeUserbot()
    # Запасной путь в LOG_CHAT есть только у владельца юзербота.
    state.me = type("U", (), {"id": OWNER})()
    state.log_entity = "log"
    state.api = None
    state.users.clear()
    approve(OWNER, OWNER_CHAT)
    state.bot_blocked = False
    yield
    state.client = state.api = state.log_entity = state.me = None
    state.users.clear()


def use_api(**kwargs):
    state.api = FakeAPI(**kwargs)
    return state.api


def test_without_bot_report_goes_to_log_chat():
    assert asyncio.run(reporter.send_report(OWNER, CARD)) is True
    assert state.client.sent[0][:2] == ("log", CARD)


def test_with_bot_report_goes_to_owner_only():
    api = use_api()
    assert asyncio.run(reporter.send_report(OWNER, CARD)) is True
    assert api.messages == [CARD]
    assert state.client.sent == [], "дублировать в лог-чат не нужно"


def test_media_is_attached_by_file_id():
    api = use_api()
    asyncio.run(reporter.send_report(OWNER, CARD, file_id="AgAC123", media_type="фото"))
    assert api.media == [("AgAC123", "фото", CARD)]
    assert api.messages == []


def test_stale_file_id_still_delivers_text():
    """file_id мог протухнуть — текст отчёта важнее вложения."""
    api = use_api(fail_media=True)
    assert asyncio.run(reporter.send_report(OWNER, CARD, file_id="dead")) is True
    assert api.messages == [CARD]


def test_blocked_bot_falls_back_and_warns_once():
    use_api(fail_text=True)
    assert asyncio.run(reporter.send_report(OWNER, CARD)) is True
    texts = [text for _, text, _ in state.client.sent]
    assert reporter.NO_START_HINT in texts, "подсказываем нажать Start"
    assert CARD in texts, "сам отчёт теряться не должен"

    asyncio.run(reporter.send_report(OWNER, "второй"))
    texts = [text for _, text, _ in state.client.sent]
    assert texts.count(reporter.NO_START_HINT) == 1, "подсказка не повторяется"


def test_recovered_bot_clears_the_flag():
    api = use_api(fail_text=True)
    asyncio.run(reporter.send_report(OWNER, CARD))
    assert state.bot_blocked is True
    api.fail_text = False
    asyncio.run(reporter.send_report(OWNER, "снова"))
    assert state.bot_blocked is False


def test_business_only_mode_without_userbot():
    """Без юзербота запасного пути нет — доставка просто не удалась."""
    state.client = None
    state.me = None
    state.log_entity = None
    use_api(fail_text=True)
    assert asyncio.run(reporter.send_report(OWNER, CARD)) is False


def test_other_owners_never_fall_back_to_the_userbot_log():
    """LOG_CHAT принадлежит владельцу юзербота — чужие отчёты туда не уходят."""
    use_api(fail_text=True)
    stranger = 999
    approve(stranger, stranger)
    assert asyncio.run(reporter.send_report(stranger, CARD)) is False
    assert state.client.sent == []
