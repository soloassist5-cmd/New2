"""Карточка правки: показать, что именно изменилось.

«Было» и «Стало» двумя абзацами заставляют сличать их глазами, а правка обычно
в одну букву. Здесь тексты слиты в один: убранное зачёркнуто, добавленное
выделено — и тесты сторожат именно это.
"""
import pytest

from bot import editlog, markup

NOW = 1_757_600_000
CHAT = {"id": 777, "type": "private", "first_name": "Mihail"}


def row(*, sent_ago=10, name="Mihail", user_id=777):
    return {"user_id": user_id, "user_name": name, "date": NOW - sent_ago}


def card(old, new, **kwargs) -> str:
    return editlog.card(row(**{k: v for k, v in kwargs.items() if k == "sent_ago"}),
                        CHAT, old, new, now=NOW,
                        count=kwargs.get("count", 1),
                        owner_id=kwargs.get("owner_id", 111))


def html(old, new, **kwargs) -> str:
    return markup.to_html(card(old, new, **kwargs))


# ------------------------------------------------------------------ диф ----

def test_a_typo_fix_shows_the_added_letter():
    assert "пр<b>и</b>вет" in html("првет", "привет")


def test_an_added_tail_is_marked_not_the_whole_text():
    out = html("буду через час", "буду через час или два")
    assert "<b> или два</b>" in out
    assert "<s>" not in out, "ничего не убирали — зачёркивать нечего"


def test_removed_words_are_struck_through():
    out = html("ты не прав нахуй", "ты не прав")
    assert "<s> нахуй</s>" in out
    assert "<b>" in out, "шапка жирная — но добавленного нет"


def test_a_long_edit_is_compared_by_words_not_letters():
    """Посимвольный диф на длинном тексте превращается в рябь."""
    old = "Зайди на сервер и спроси в вип, ответят нету, это давно известная вещь"
    new = "Зайди на любой сервер и спроси у адм, ответят нету, это известная вещь"
    merged = editlog.merge(old, new)
    assert merged is not None
    assert "любой" in merged and "сервер" in merged


def test_completely_different_texts_fall_back_to_two_blocks():
    """Сливать нечего — слитый вид был бы кашей."""
    out = html("во сколько встречаемся?", "ладно, забей, я сам разберусь позже")
    assert "Было:" in out and "Стало:" in out
    assert editlog.merge("во сколько встречаемся?",
                         "ладно, забей, я сам разберусь позже") is None


def test_merge_refuses_empty_sides():
    assert editlog.merge("", "привет") is None
    assert editlog.merge("привет", "") is None


# --------------------------------------------------------------- частные ---

def test_added_caption_is_named_as_such():
    out = html("", "это я на море")
    assert "Подпись добавлена" in out and "это я на море" in out


def test_removed_text_is_named_as_such():
    out = html("секретное сообщение", "")
    assert "убран" in out and "секретное сообщение" in out


def test_multiline_edit_survives_the_quote():
    out = html("первая\nвторая", "первая\nтретья")
    assert "<blockquote>" in out and "</blockquote>" in out
    assert out.count("<blockquote>") == 1, "цитата одна, а не по строке"


# ------------------------------------------------------------- разметка ----

@pytest.mark.parametrize("text", [
    "цена **100** руб", "это _важно_", "код `x = 1`", "> цитата",
    "ссылка [тут](http://a.b)", "~~зачёркнуто~~", "<b>тег</b>",
])
def test_users_markdown_is_shown_verbatim(text):
    """`**` в чужом сообщении — это символы, а не приказ сделать жирным."""
    out = html(text, text + " ещё")
    assert "<b> ещё</b>" in out, "наша разметка работает"
    for marker in ("**", "_", "`", "~~"):
        if marker in text:
            assert marker in out, f"{marker} должен остаться на месте"
    if "<b>тег</b>" == text:
        assert "&lt;b&gt;" in out, "html из чужого текста экранируется"


def test_service_markers_never_reach_a_message():
    """Метки дословного текста служебные — в сообщении их быть не должно."""
    out = card("првет", "привет")
    assert "\x02" in out, "внутри карточки метки есть"
    assert "\x02" not in markup.to_html(out) and "\x03" not in markup.to_html(out)
    assert "\x02" not in markup.strip_raw(out)


# --------------------------------------------------------------- шапка -----

def test_a_quick_fix_is_called_a_quick_fix():
    assert "через 8 секунд" in card("а", "б", sent_ago=8)


def test_a_late_edit_names_both_times():
    out = card("привет как дела", "привет как дела?", sent_ago=7200)
    assert "отправлено" in out and "спустя" in out


def test_repeat_edits_are_counted():
    assert "правка №3" in card("а", "аб", count=3)
    assert "правка №" not in card("а", "аб", count=1), "первая правка — просто правка"


def test_own_edit_is_signed_as_ours():
    out = editlog.card(row(user_id=111), CHAT, "првет", "привет", now=NOW,
                       owner_id=111)
    assert out.startswith("✏️ **Правка** · Вы")


def test_the_chat_is_not_repeated_after_the_sender():
    """В личке чат и собеседник — одно и то же; писать дважды незачем."""
    assert card("а", "аб").count("Mihail") == 1
