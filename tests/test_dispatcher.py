"""Разбор команд и реестр."""
import config
from core import dispatcher
from modules.mute import _final_text


def test_pattern_matches_only_prefixed_commands():
    pattern = dispatcher.build_pattern()
    match = pattern.match(".mute 10m шумит")
    assert match.group(1) == "mute" and match.group(2) == "10m шумит"
    assert pattern.match(".mute").group(2) is None
    assert pattern.match("mute") is None
    assert pattern.match("..mute") is None
    assert pattern.match("текст с .mute внутри") is None


def test_multiline_arguments_are_kept():
    match = dispatcher.build_pattern().match(".save шаблон строка1\nстрока2")
    assert match.group(2) == "шаблон строка1\nстрока2"


def test_split_args_separates_flags():
    args, flags = dispatcher.split_args("@vasya 10m шумит -q")
    assert args == ["@vasya", "10m", "шумит"] and flags == {"q"}


def test_negative_chat_id_is_not_a_flag():
    args, flags = dispatcher.split_args("-100500")
    assert args == ["-100500"] and flags == set()


def test_registry_is_complete_and_unique():
    import modules  # noqa: F401 — импорт регистрирует команды
    assert len(dispatcher.COMMANDS) >= 40
    names = [c.name for c in dispatcher.COMMANDS]
    assert len(names) == len(set(names))
    for name in ("mute", "unmute", "gmute", "deleted", "restore", "purge", "help"):
        assert name in dispatcher.REGISTRY


def test_help_lists_categories_and_single_command():
    text = dispatcher.help_text()
    assert "Мут" in text and "Антиудаление" in text and len(text) <= 4000
    # Разделы идут в заданном порядке, а не в том, как линтер разложил импорты.
    positions = [text.index(f"**{cat}**") for cat in dispatcher.CAT_ORDER]
    assert positions == sorted(positions)
    one = dispatcher.help_text("mute")
    assert one.startswith(f"**{config.PREFIX}mute**")
    assert "не найдена" in dispatcher.help_text("несуществующая")


def test_mute_texts():
    assert _final_text(is_peer=True, who="X", seconds=0) == \
        "🔇 **Вы были замучены на неопределённый срок.**"
    assert _final_text(is_peer=True, who="X", seconds=600) == \
        "🔇 **Вы были замучены на 10 минут.**"
    text = _final_text(is_peer=False, who="Вася", seconds=7200, global_=True)
    assert "Вася замучен(а) на 2 часа" in text and "во всех чатах" in text
