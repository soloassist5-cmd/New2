"""Анимация правки сообщения: кадры, срыв анимации, экранирование разметки."""
import asyncio

import pytest
from telethon.errors import FloodWaitError, MessageNotModifiedError

from core import anim

FINAL = "🔇 **Вы были замучены на неопределённый срок.**"


class FakeMessage:
    """Заглушка Telethon-сообщения, записывающая каждый кадр."""

    def __init__(self, fail_at=None, error=None):
        self.frames: list[str] = []
        self.fail_at, self.error, self.calls = fail_at, error, 0

    async def edit(self, text, link_preview=False):
        self.calls += 1
        if self.fail_at is not None and self.calls >= self.fail_at:
            raise self.error
        if self.frames and self.frames[-1] == text:
            raise MessageNotModifiedError(None)
        self.frames.append(text)
        return self


def play(msg, final=FINAL, style="type"):
    asyncio.run(anim.play(msg, final, style=style, delay=0))
    return msg.frames


def test_type_style_frames():
    frames = play(FakeMessage())
    assert frames[0].startswith("🔇 ▱")            # старт — пустая полоса
    assert any(anim.CURSOR in f for f in frames)   # был курсор
    assert frames[-1] == FINAL                     # финал — с разметкой


def test_intermediate_frames_have_no_markdown():
    """Незакрытые ** / _ в промежуточном кадре Telegram показал бы как есть."""
    for frame in play(FakeMessage())[:-1]:
        assert "**" not in frame and "_" not in frame


def test_bar_style_ends_with_final():
    frames = play(FakeMessage(), style="bar")
    assert frames[-1] == FINAL
    assert all(f.startswith("🔇") for f in frames)


def test_off_style_is_single_edit():
    assert play(FakeMessage(), style="off") == [FINAL]


def test_long_text_is_chunked():
    long = FINAL + "\n_Причина: слишком много сообщений подряд, капслок и стикеры_"
    frames = play(FakeMessage(), final=long)
    assert len(frames) <= 3 + anim.MAX_TYPE_FRAMES + 1
    assert frames[-1] == long


def test_flood_wait_aborts_but_final_text_survives():
    error = FloodWaitError(request=None)
    error.seconds = 60
    msg = FakeMessage()

    async def edit(text, link_preview=False):
        msg.calls += 1
        if msg.calls == 3:
            raise error
        msg.frames.append(text)
        return msg

    msg.edit = edit
    asyncio.run(anim.play(msg, FINAL, style="type", delay=0))
    assert msg.frames[-1] == FINAL, "результат важнее спецэффекта"


def test_missing_message_does_not_raise():
    class Gone(FakeMessage):
        async def edit(self, text, link_preview=False):
            raise RuntimeError("message gone")

    asyncio.run(anim.play(Gone(), FINAL, style="type", delay=0))  # не должно упасть


@pytest.mark.parametrize("src,want", [
    ("🔇 **Вы были замучены.**", "🔇 Вы были замучены."),
    ("**[Вася](tg://user?id=1) замучен(а).**\n_(во всех чатах)_",
     "Вася замучен(а).\n(во всех чатах)"),
    ("_Причина: спам_", "Причина: спам"),
    ("файл snake_case_name.py", "файл snake_case_name.py"),   # подчёркивания в словах живут
    ("`код`", "код"),
])
def test_strip_md(src, want):
    assert anim.strip_md(src) == want


def test_bar():
    assert anim.bar(0, 4, 8) == "▱" * 8
    assert anim.bar(4, 4, 8) == "▰" * 8
    assert anim.bar(2, 4, 8) == "▰▰▰▰▱▱▱▱"
