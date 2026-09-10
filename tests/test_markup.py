"""Разметка Telethon -> HTML для Bot API."""
import pytest

from bot import markup


@pytest.mark.parametrize("src,want", [
    ("**жирный**", "<b>жирный</b>"),
    ("_курсив_", "<i>курсив</i>"),
    ("`код`", "<code>код</code>"),
    ("~~зачёркнуто~~", "<s>зачёркнуто</s>"),
    ("[текст](https://ex.com)", '<a href="https://ex.com">текст</a>'),
])
def test_basic_markup(src, want):
    assert markup.to_html(src) == want


def test_html_special_chars_are_escaped():
    assert markup.to_html("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_code_content_is_escaped_but_not_parsed():
    assert markup.to_html("`<b>не тег</b>`") == "<code>&lt;b&gt;не тег&lt;/b&gt;</code>"


def test_fenced_block_becomes_pre():
    assert markup.to_html("```\nTraceback <module>\n```") == \
        "<pre>\nTraceback &lt;module&gt;\n</pre>"


def test_underscores_inside_words_survive():
    assert markup.to_html("файл snake_case_name.py") == "файл snake_case_name.py"


def test_link_url_is_not_touched_by_italics():
    assert markup.to_html("[тут](https://ex.com/a_b_c)") == \
        '<a href="https://ex.com/a_b_c">тут</a>'


def test_mention_inside_bold():
    assert markup.to_html("**[Вася](tg://user?id=1) замучен**") == \
        '<b><a href="tg://user?id=1">Вася</a> замучен</b>'


def test_empty_text():
    assert markup.to_html("") == ""


def test_trim_closes_open_tags():
    """Обрезанный HTML с повисшим тегом Telegram отклоняет целиком."""
    assert markup.trim("<b>очень длинный</b> хвост", 14) == "<b>очень длин…</b>"


def test_trim_keeps_short_text_intact():
    assert markup.trim("<b>а</b>", 100) == "<b>а</b>"


def test_trim_does_not_split_entities():
    assert "&" not in markup.trim("текст &amp; ещё", 9)


def test_trim_nested_tags():
    out = markup.trim("<b><i>длинный текст внутри</i></b>", 20)
    assert out.endswith("</i></b>")


def test_quote_block_becomes_blockquote():
    assert markup.to_html("> первая\n> вторая") == \
        "<blockquote>первая\nвторая</blockquote>"


def test_quote_keeps_inner_markup():
    assert markup.to_html("> **важно**") == "<blockquote><b>важно</b></blockquote>"


def test_quote_does_not_swallow_the_rest():
    out = markup.to_html("> цитата\n\nобычный текст")
    assert out == "<blockquote>цитата</blockquote>\n\nобычный текст"


def test_italic_does_not_span_lines():
    """Незакрытый `_` в многострочном тексте иначе съедал бы полдокумента."""
    assert markup.to_html("_первая\nвторая_") == "_первая\nвторая_"
