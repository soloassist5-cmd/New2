"""Необязательные команды: стикеры, кубики, мелкая анимация."""
import asyncio
import io

import pytest
from PIL import Image

import config
import db
from bot import dotcmd, fun, funcmd
from core import chatprefs, state
from tests.fakes import ALL_RIGHTS, FakeBotAPI, approve, business_message

OWNER, PEER, BIZ = 111, 777, "biz1"


@pytest.fixture(autouse=True)
def env():
    config.DB_PATH.unlink(missing_ok=True)
    asyncio.run(db.init())
    chatprefs.invalidate_all()
    config.OWNER_ID = OWNER
    funcmd.SPEED = 0                      # анимацию в тестах не ждём
    state.users.clear()
    state.business.clear()
    state.mutes.clear()
    state.forget_own_deletions()
    approve(OWNER, OWNER)
    state.business[BIZ] = {"user_id": OWNER, "user_chat_id": OWNER,
                           "is_enabled": True, "rights": dict(ALL_RIGHTS)}
    state.api = FakeBotAPI()
    yield
    funcmd.SPEED = 1.0
    config.OWNER_ID = 0
    asyncio.run(db.close())
    state.api = None
    state.business.clear()


def run(text, msg_id=5):
    asyncio.run(dotcmd.handle(
        state.api,
        business_message(text, connection_id=BIZ, chat_id=PEER, from_id=OWNER,
                         message_id=msg_id),
        BIZ, OWNER))
    return state.api


def posted(api) -> list[str]:
    """Что появилось в самой переписке."""
    return [text for _, text, biz in api.sent if biz == BIZ]


def frames(api) -> list[str]:
    return posted(api) + [text for _, _, text in api.edits]


# ---------------------------------------------------------------- стикеры --

def test_emoji_renders_to_a_transparent_sticker():
    """Фото Telegram сплющит на белый фон — прозрачность держит только стикер."""
    payload = fun.render("🌹")
    assert payload and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP"

    image = Image.open(io.BytesIO(payload))
    assert image.mode == "RGBA"
    assert max(image.size) == 512, "одна сторона ровно 512 — требование Telegram"
    assert image.getpixel((0, 0))[3] == 0, "угол прозрачный"


def test_rose_sends_a_sticker_and_removes_the_command():
    api = run(".rose")
    assert api.sticker_uploads, "первый раз файл загружается"
    assert api.all_deleted_ids == [5], "сама команда из переписки убрана"
    assert posted(api) == [], "текстом ничего не пишем"


def test_the_same_sticker_is_not_uploaded_twice():
    run(".rose", msg_id=5)
    uploads = len(state.api.sticker_uploads)
    run(".rose", msg_id=6)
    assert len(state.api.sticker_uploads) == uploads, "второй раз идёт готовый file_id"
    assert state.api.stickers, "отправлен по сохранённому id"


def test_any_emoji_works():
    api = run(".e 🦊")
    assert api.sticker_uploads


def test_text_instead_of_emoji_is_refused():
    api = run(".e привет")
    assert any("Нужен один эмодзи" in text for text in api.texts_to(OWNER))
    assert api.sticker_uploads == []


def test_missing_font_is_explained_not_crashed(monkeypatch):
    monkeypatch.setattr(fun, "FONT_PATHS", ())
    api = run(".rose")
    assert any("шрифт" in text for text in api.texts_to(OWNER))


@pytest.mark.parametrize("emoji", ["🌹", "❤️", "🔥", "🐱", "🎉", "🤡"])
def test_every_shortcut_emoji_can_be_drawn(emoji):
    assert fun.render(emoji), f"{emoji} не отрисовался"


# ----------------------------------------------------------------- кубики --

@pytest.mark.parametrize("command,emoji", [
    (".dice", "🎲"), (".dart", "🎯"), (".basket", "🏀"),
    (".foot", "⚽"), (".bowl", "🎳"), (".slot", "🎰"),
])
def test_dice_uses_telegram_animation(command, emoji):
    api = run(command)
    assert api.dice == [(PEER, emoji, BIZ)]
    assert api.all_deleted_ids == [5]


# -------------------------------------------------------------- анимация --

def test_type_prints_the_text():
    api = run(".type привет как дела")
    assert frames(api)[-1] == "привет как дела"
    assert len(api.edits) > 1, "текст появляется постепенно"


def test_type_without_text_is_refused():
    api = run(".type")
    assert any("Что печатать" in text for text in api.texts_to(OWNER))


def test_countdown_ends_with_launch():
    api = run(".countdown 3")
    shown = frames(api)
    assert shown[0] == "3️⃣" and "Поехали" in shown[-1]
    assert len(shown) == 4


def test_countdown_is_bounded():
    api = run(".countdown 99")
    assert frames(api)[0] == "🔟", "больше десяти не отсчитываем"


def test_love_grows():
    api = run(".love")
    assert frames(api)[-1] == "❤️‍🔥"


def test_flip_lands_on_a_side():
    api = run(".flip")
    assert any(side in frames(api)[-1] for side in ("Орёл", "Решка"))


# ----------------------------------------------------------- случайности --

@pytest.mark.parametrize("spec,count,sides", [
    ("1d6", 1, 6), ("2d6", 2, 6), ("d20", 1, 20), ("", 1, 6), ("3d10", 3, 10),
])
def test_roll_understands_the_notation(spec, count, sides):
    throws, total, rolled_sides = fun.roll(spec)
    assert len(throws) == count and rolled_sides == sides
    assert total == sum(throws)
    assert all(1 <= value <= sides for value in throws)


@pytest.mark.parametrize("spec", ["abc", "0d6", "100d6", "2d1", "2d99999"])
def test_roll_refuses_nonsense(spec):
    assert fun.roll(spec) is None


def test_roll_shows_every_die():
    api = run(".roll 3d6")
    assert "3d6" in frames(api)[-1] and "+" in frames(api)[-1]


def test_roll_with_bad_notation_explains():
    api = run(".roll абвгд")
    assert any("Не понял запись" in text for text in api.texts_to(OWNER))


def test_eight_ball_repeats_the_question_and_answers():
    api = run(".8ball полетим в отпуск?")
    final = frames(api)[-1]
    assert "полетим в отпуск?" in final
    assert any(answer in final for answer in fun.EIGHT_BALL)


def test_eight_ball_needs_a_question():
    api = run(".8ball")
    assert any("А вопрос?" in text for text in api.texts_to(OWNER))


def test_choose_picks_one_of_the_options():
    api = run(".choose чай | кофе | сон")
    assert any(option in frames(api)[-1] for option in ("чай", "кофе", "сон"))


def test_choose_needs_at_least_two():
    api = run(".choose только одно")
    assert any("два варианта" in text for text in api.texts_to(OWNER))


# ------------------------------------------------------------- в справке --

def test_fun_commands_are_grouped_separately():
    text = dotcmd.help_text()
    assert "**Развлечения**" in text
    assert text.index("**Мут**") < text.index("**Развлечения**")


def test_fun_commands_are_visible_in_the_chat():
    for name in ("rose", "dice", "roll", "8ball"):
        assert dotcmd.REGISTRY[name].visible, name
