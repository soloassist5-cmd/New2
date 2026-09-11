"""Ритм анимации: кадры должны идти ровно, а не «раз в паузу плюс связь».

Тесты здесь работают в реальном времени — на заглушке с нулевой задержкой
разницы между старым и новым поведением не видно вовсе, а именно она и была
багом: заказанные 0.6 с превращались в 0.6 + время правки.
"""
import asyncio
import time

import pytest

from core import anim


class Link:
    """Сообщение, правка которого занимает заданное время, как настоящая связь."""

    def __init__(self, cost: float = 0.0, costs: list[float] | None = None):
        self.cost, self.costs = cost, list(costs or [])
        self.frames: list[str] = []
        self.at: list[float] = []

    async def edit(self, text, link_preview=False):
        await asyncio.sleep(self.costs.pop(0) if self.costs else self.cost)
        self.frames.append(text)
        self.at.append(time.monotonic())
        return self

    @property
    def gaps(self) -> list[float]:
        """Через сколько кадр сменялся у собеседника."""
        return [b - a for a, b in zip(self.at, self.at[1:], strict=False)]


def play(msg, frames, **kw) -> Link:
    asyncio.run(anim.play_frames(msg, frames, **kw))
    return msg


FOUR = ["а", "б", "в", "г"]


def test_frame_rate_matches_the_delay_asked_for():
    """Правка съедает 0.2 с — но кадры всё равно идут раз в 0.4, а не в 0.6."""
    msg = play(Link(cost=0.2), FOUR, delay=0.4)
    assert msg.frames == FOUR
    for gap in msg.gaps:
        assert 0.34 < gap < 0.47, f"шаг {gap:.2f} — это delay + связь, а не delay"


def test_slow_link_sets_the_tempo_instead_of_piling_up():
    """Связь медленнее заказанного темпа — идём её скоростью, но ровно."""
    msg = play(Link(cost=0.5), FOUR, delay=0.2)
    assert msg.frames == FOUR
    for gap in msg.gaps:
        assert 0.5 <= gap < 0.68, f"шаг {gap:.2f} — это связь плюс пауза сверху"
    assert max(msg.gaps) - min(msg.gaps) < 0.12, "рывков быть не должно"


def test_a_slow_edit_does_not_drag_the_rest_of_the_animation():
    """Внезапно медленную правку не предскажешь — но копиться отставание не должно."""
    msg = play(Link(costs=[0.05, 0.45, 0.05, 0.05]), FOUR, delay=0.5)
    assert msg.frames == FOUR
    assert msg.gaps[1] < 0.62, f"после рывка темп не вернулся: {msg.gaps[1]:.2f}"
    assert msg.gaps[2] < 0.62, f"отставание копится: {msg.gaps[2]:.2f}"


def test_slow_link_is_noticed_from_the_first_frame():
    """Связь просела — темп сбавляем сразу, а не после второго рывка."""
    msg = play(Link(costs=[0.05, 0.6, 0.6, 0.6]), FOUR, delay=0.3)
    for gap in msg.gaps[1:]:
        assert gap < 0.85, f"шаг {gap:.2f}: правки идут вплотную к прошлой"


def test_fast_frames_are_floored_to_what_telegram_redraws():
    """Просить 0.05 с бессмысленно: клиент столько кадров не отрисует."""
    msg = play(Link(), ["а", "б", "в"], delay=0.05)
    for gap in msg.gaps:
        assert gap >= anim.MIN_FRAME - 0.02, f"шаг {gap:.2f} быстрее допустимого"


def test_settle_gives_the_sent_message_time_to_appear():
    """Отправка и правка вплотную — клиент склеит их, и первый кадр пропадёт."""
    started = time.monotonic()
    play(Link(), ["а", "б"], delay=0.4, settle=0.3)
    assert time.monotonic() - started > 0.6, "паузы после отправки не было"


def test_zero_delay_plays_instantly():
    """Тестовый и выключенный режим: кадры без единой паузы."""
    started = time.monotonic()
    msg = play(Link(), FOUR, delay=0, settle=0.5)
    assert msg.frames == FOUR
    assert time.monotonic() - started < 0.1


def test_repeated_frames_collapse():
    """Правка «в тот же текст» — лишний запрос и лишняя пауза на пустом месте."""
    msg = play(Link(), ["а", "а", "б", "б", "б"], delay=0)
    assert msg.frames == ["а", "б"]


def test_last_frame_always_shows():
    msg = play(Link(), ["а", "б", "финал"], delay=0)
    assert msg.frames[-1] == "финал"


@pytest.mark.parametrize("delay,cost,want", [
    (0.0, 0.0, 0.0),                       # анимация выключена
    (0.6, 0.1, 0.6),                       # связь быстрее темпа — темп наш
    (0.2, 0.9, 0.9 * anim.PACE_FACTOR),    # связь медленнее — темп её
    (0.1, 0.0, anim.MIN_FRAME),            # ниже порога отрисовки не опускаемся
    (0.5, 9.0, anim.MAX_FRAME),            # одна зависшая правка не тянет всё
])
def test_pace(delay, cost, want):
    assert anim._pace(delay, cost) == pytest.approx(want)


def test_type_out_keeps_the_final_markup():
    msg = Link()
    asyncio.run(anim.type_out(msg, "**жирный** финал", delay=0, settle=0))
    assert msg.frames[-1] == "**жирный** финал"
    assert all("**" not in frame for frame in msg.frames[:-1])
