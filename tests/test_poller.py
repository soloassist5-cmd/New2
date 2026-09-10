"""Маршрутизация апдейтов Bot API."""
import asyncio

import pytest

from bot import poller


@pytest.fixture
def routed(monkeypatch):
    calls = []

    def record(name):
        async def handler(*args):
            calls.append((name, args[-1]))
        return handler

    monkeypatch.setattr(poller.business, "on_business_connection",
                        record("connection"))
    monkeypatch.setattr(poller.business, "on_business_message", record("message"))
    monkeypatch.setattr(poller.business, "on_edited_business_message", record("edited"))
    monkeypatch.setattr(poller.business, "on_deleted_business_messages",
                        record("deleted"))
    monkeypatch.setattr(poller.commands, "handle", record("command"))
    return calls


@pytest.mark.parametrize("key,expected", [
    ("business_connection", "connection"),
    ("business_message", "message"),
    ("edited_business_message", "edited"),
    ("deleted_business_messages", "deleted"),
    ("message", "command"),
])
def test_each_update_kind_reaches_its_handler(routed, key, expected):
    asyncio.run(poller.dispatch(None, {"update_id": 1, key: {"payload": True}}))
    assert routed == [(expected, {"payload": True})]


def test_unknown_update_kind_is_ignored(routed):
    asyncio.run(poller.dispatch(None, {"update_id": 1, "poll_answer": {}}))
    assert routed == []


def test_business_updates_are_requested_explicitly():
    """Без allowed_updates Telegram не пришлёт ни одного business-события."""
    for name in ("business_connection", "business_message",
                 "edited_business_message", "deleted_business_messages"):
        assert name in poller.ALLOWED_UPDATES
