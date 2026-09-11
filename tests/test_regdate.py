"""Оценка даты регистрации по номеру аккаунта.

Точность здесь не проверить — её и нет, это интерполяция между якорями.
Проверяем то, что обязано выполняться: порядок, границы и честность формулировок.
"""
import datetime as dt

import pytest

from bot import regdate


def year_of(unix: int) -> int:
    return dt.datetime.fromtimestamp(unix).year


def test_anchors_are_sorted_by_id_and_by_date():
    """На паре, где больший id старше меньшего, интерполяция даёт бессмыслицу."""
    ids = [uid for uid, _ in regdate.ANCHORS]
    dates = [at for _, at in regdate.ANCHORS]
    assert ids == sorted(ids) and len(set(ids)) == len(ids)
    assert dates == sorted(dates)


def test_every_anchor_estimates_to_its_own_date():
    for uid, at in regdate.ANCHORS[1:-1]:
        guess = regdate.estimate(uid)
        assert guess and abs(guess["at"] - at) <= 86400, uid


def test_bigger_id_never_means_older_account():
    previous = 0
    for uid in (1, 10_000, 1_000_000, 120_000_000, 800_000_000,
                1_500_000_000, 5_000_000_000, 7_000_000_000):
        guess = regdate.estimate(uid)
        assert guess["at"] >= previous, uid
        previous = guess["at"]


@pytest.mark.parametrize("bad", [0, -1, -100_500_000_000])
def test_non_user_ids_are_refused(bad):
    assert regdate.estimate(bad) is None
    assert regdate.words(bad) == ""


def test_ids_newer_than_the_table_say_so_instead_of_guessing():
    """Экстраполировать за последний якорь — значит выдумывать."""
    guess = regdate.estimate(regdate.LAST_ID + 10**9)
    assert guess["after"] is True
    assert "после" in regdate.words(regdate.LAST_ID + 10**9)


def test_the_first_accounts_land_in_2013():
    assert year_of(regdate.estimate(1)["at"]) == 2013


def test_wording_hedges_and_names_a_month():
    text = regdate.words(120_000_000)
    assert "примерно" in text and "2015" in text


def test_a_wide_bracket_is_disclosed():
    """Между соседними якорями бывает больше полугода — молчать об этом нечестно."""
    widest = max(zip(regdate.ANCHORS, regdate.ANCHORS[1:], strict=False),
                 key=lambda pair: pair[1][1] - pair[0][1])
    (low_id, low_at), (high_id, high_at) = widest
    if high_at - low_at <= regdate.WIDE:
        pytest.skip("в таблице нет вилки шире полугода")
    assert "точнее" in regdate.words((low_id + high_id) // 2)


def test_month_names_are_declined_for_their_phrases():
    someone = regdate.ANCHORS[len(regdate.ANCHORS) // 2][1]
    assert regdate.month_in(someone).split()[0].endswith(("е", "и"))
    assert regdate.month_of(someone).split()[0].endswith(("я", "а"))
