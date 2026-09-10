"""Парсинг сроков, склонения и форматирование."""
import pytest

from core import fmt


@pytest.mark.parametrize("raw,want", [
    ("10m", 600), ("2h", 7200), ("1d", 86400), ("30s", 30),
    ("15", 900),            # без единицы — минуты
    ("3мин", 180), ("2ч", 7200), ("1д", 86400), ("1w", 604800),
    ("завтра", None), ("", None), ("10 m", 600),
])
def test_parse_duration(raw, want):
    assert fmt.parse_duration(raw) == want


@pytest.mark.parametrize("sec,want", [
    (45, "45 секунд"), (60, "1 минуту"), (120, "2 минуты"), (300, "5 минут"),
    (600, "10 минут"), (3600, "1 час"), (7200, "2 часа"), (18000, "5 часов"),
    (86400, "1 день"), (172800, "2 дня"), (604800, "1 неделю"),
])
def test_human_delta(sec, want):
    assert fmt.human_delta(sec) == want


@pytest.mark.parametrize("sec,want", [
    (3660, "1 час 1 минуту"),        # не «61 минуту»
    (90000, "1 день 1 час"),         # не «25 часов»
    (5400, "1 час 30 минут"),
    (1209600, "2 недели"),
    (1800, "30 минут"),
])
def test_human_delta_composite(sec, want):
    assert fmt.human_delta(sec) == want


def test_plural():
    assert fmt.plural(1, ("час", "часа", "часов")) == "час"
    assert fmt.plural(3, ("час", "часа", "часов")) == "часа"
    assert fmt.plural(11, ("час", "часа", "часов")) == "часов"
    assert fmt.plural(21, ("час", "часа", "часов")) == "час"


def test_uptime_and_size():
    assert fmt.uptime(90061).startswith("1д")
    assert fmt.uptime(3661) == "1ч 1м 1с"
    assert fmt.size(1536) == "1.5 КБ"
    assert fmt.size(512) == "512 Б"


def test_truncate():
    assert fmt.truncate("абвгд", 3) == "аб…"
    assert fmt.truncate("абв", 10) == "абв"
    assert fmt.truncate(None) == ""
