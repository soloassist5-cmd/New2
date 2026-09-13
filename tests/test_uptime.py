"""Учёт запусков: отчёт о старте не должен превращаться в ленту перезапусков."""
import asyncio

import pytest

import config
import db
from core import uptime

HOUR = 3600
DAY = 86400
NOW = 1_789_000_000


@pytest.fixture(autouse=True)
def env():
    config.DB_PATH.unlink(missing_ok=True)
    asyncio.run(db.init())
    config.STARTUP_QUIET_SEC = 6 * HOUR
    config.RESTART_ALERT = 6
    yield
    asyncio.run(db.close())


def start(at):
    return asyncio.run(uptime.note_start(at))


def test_the_very_first_start_is_always_reported():
    first = start(NOW)
    assert first["quiet"] is False
    assert first["per_day"] == 1 and first["previous"] == 0


def test_a_restart_right_after_the_previous_one_stays_quiet():
    start(NOW)
    again = start(NOW + 10 * 60)
    assert again["quiet"] is True, "десять минут — это не новость"
    assert again["gap"] == 600


def test_a_start_after_a_long_break_is_reported():
    start(NOW)
    later = start(NOW + 7 * HOUR)
    assert later["quiet"] is False


def test_starts_are_counted_over_the_day():
    for i in range(5):
        start(NOW + i * 600)
    assert asyncio.run(uptime.starts_per_day(NOW + 5 * 600)) == 5


def test_yesterdays_starts_do_not_count():
    start(NOW - 2 * DAY)
    start(NOW - DAY - HOUR)
    assert asyncio.run(uptime.starts_per_day(NOW)) == 0


def test_the_log_does_not_grow_without_bound():
    for i in range(uptime.MAX_KEPT + 50):
        start(NOW + i)
    kept = asyncio.run(uptime._recent(NOW + uptime.MAX_KEPT + 50))
    assert len(kept) <= uptime.MAX_KEPT + 1


# ------------------------------------------------------------- диагноз -----

def test_a_calm_bot_says_nothing_about_restarts():
    assert uptime.note(2) == ""
    assert uptime.restless(2) is False


def test_frequent_restarts_are_explained_not_hidden():
    text = uptime.note(40)
    assert "40" in text
    assert "засыпает" in text, "надо объяснить, а не просто напугать числом"
    assert "/health" in text, "и сказать, чем лечится"


def test_the_threshold_is_configurable():
    config.RESTART_ALERT = 100
    assert uptime.note(40) == ""


def test_a_broken_log_does_not_break_the_start():
    """В kv могло остаться что угодно — падать на этом нельзя."""
    asyncio.run(db.kv_set(uptime.KEY_LOG, "мусор,,17,завтра"))
    assert start(NOW)["per_day"] >= 1
