"""Очистка переписки: одна сводка с файлом вместо сотни карточек."""
import asyncio

import pytest

import config
import db
from bot import business, transcript
from core import state
from tests.fakes import ALL_RIGHTS, FakeBotAPI, approve, business_message

OWNER = 111
OWNER_CHAT = 111
PEER = 777
BIZ = "biz1"


@pytest.fixture(autouse=True)
def env():
    from core.chatprefs import invalidate_all

    config.DB_PATH.unlink(missing_ok=True)
    asyncio.run(db.init())
    invalidate_all()
    config.PURGE_DEBOUNCE_SEC = 0
    config.PURGE_THRESHOLD = 5
    config.PURGE_MEDIA_LIMIT = 10
    business._pending.clear()
    state.business.clear()
    state.mutes.clear()
    state.forget_own_deletions()
    state.users.clear()
    state.allowlist.clear()
    config.OWNER_ID = OWNER      # владелец бота — он же администратор
    approve(OWNER, OWNER_CHAT)
    state.client = None
    state.log_entity = None
    state.business[BIZ] = {"user_id": OWNER, "user_chat_id": OWNER_CHAT,
                           "is_enabled": True, "rights": dict(ALL_RIGHTS)}
    state.api = FakeBotAPI()
    yield
    config.OWNER_ID = 0
    asyncio.run(db.close())
    state.api = None
    business._pending.clear()


def deleted_update(ids):
    return {"business_connection_id": BIZ,
            "chat": {"id": PEER, "type": "private", "first_name": "Вася"},
            "message_ids": list(ids)}


def talk(count, *, start=1, photo_every=0):
    """Наполняет чат перепиской: чётные сообщения — от владельца."""
    async def fill():
        for offset in range(count):
            msg_id = start + offset
            kwargs = {"message_id": msg_id, "date": 1700000000 + offset * 60}
            if offset % 2:
                kwargs["from_id"] = OWNER
                kwargs["first_name"] = "Владелец"
            if photo_every and offset % photo_every == 0:
                kwargs["photo"] = f"AgAC{msg_id}"
            await business.on_business_message(
                state.api, business_message(f"сообщение {msg_id}", **kwargs))
    asyncio.run(fill())


def purge(ids):
    asyncio.run(business.on_deleted_business_messages(state.api, deleted_update(ids)))


# --------------------------------------------------------------- пороги -----

def test_few_deletions_stay_separate_cards():
    talk(3)
    state.api.sent.clear()
    purge([1, 2, 3])
    assert state.api.files == [], "файл ради трёх сообщений не нужен"
    assert sum("Удалённое сообщение" in text for text in state.api.texts) == 3


def test_mass_deletion_produces_one_file_and_one_summary():
    talk(12)
    state.api.sent.clear()
    purge(range(1, 13))

    assert len(state.api.files) == 1, "ровно один файл"
    chat_id, name = state.api.files[0]
    assert chat_id == OWNER_CHAT and name.endswith(".txt")
    assert not [t for t in state.api.texts if "Удалённое сообщение" in t], \
        "поштучных карточек быть не должно"


def test_summary_counts_everything():
    talk(12)
    state.api.sent.clear()
    purge(range(1, 13))
    summary = state.api.captions[0]
    assert "Переписка очищена" in summary
    assert "**12**" in summary and "Вася" in summary


def test_messages_move_to_the_journal():
    talk(12)
    purge(range(1, 13))
    assert len(asyncio.run(db.last_deleted(OWNER, PEER, 100))) == 12
    assert asyncio.run(db.get_message(OWNER, PEER, 1)) is None, "кэш вычищен"


# ------------------------------------------------------------ содержимое ----

def transcript_text():
    return state.api.uploads[0].decode("utf-8")


def test_transcript_keeps_order_and_authors():
    talk(12)
    purge(range(1, 13))
    text = transcript_text()
    assert text.index("сообщение 1") < text.index("сообщение 12"), "по времени"
    assert "Вася" in text and "Вы" in text, "свои сообщения подписаны иначе"


def test_transcript_marks_media():
    talk(12, photo_every=4)
    purge(range(1, 13))
    assert "<фото>" in transcript_text()


def test_transcript_reports_what_was_not_cached():
    """Часть переписки старше TTL или пришла до подключения бота."""
    talk(6)
    state.api.sent.clear()
    purge(range(1, 21))                 # удалили 20, в кэше только 6
    text = transcript_text()
    assert "Удалено сообщений: 20" in text
    assert "Восстановлено из кэша: 6" in text
    assert "Не было в кэше: 14" in text
    assert "не было в кэше: 14" in state.api.captions[0]


def test_empty_cache_reports_the_purge_without_an_empty_file():
    purge(range(1, 30))
    assert state.api.files == [], "пустой файл отправлять незачем"
    assert any("Удалено сообщений: 29" in text for text in state.api.texts)


# ----------------------------------------------------------------- медиа ----

def test_media_is_resent_after_the_file():
    talk(12, photo_every=3)
    state.api.sent.clear()
    purge(range(1, 13))
    assert state.api.media, "вложения приходят следом за файлом"
    assert all(item[1].startswith("AgAC") for item in state.api.media)


def test_media_flood_is_capped():
    config.PURGE_MEDIA_LIMIT = 3
    talk(12, photo_every=1)
    state.api.sent.clear()
    purge(range(1, 13))
    assert len(state.api.media) == 3
    assert "присылаю 3" in state.api.captions[0]


# --------------------------------------------------------------- пачками ----

def test_updates_within_the_window_merge_into_one_report():
    """Telegram дробит очистку на несколько апдейтов — сводка должна быть одна."""
    config.PURGE_DEBOUNCE_SEC = 5
    talk(12)
    state.api.sent.clear()

    async def scenario():
        await business.on_deleted_business_messages(state.api, deleted_update(range(1, 7)))
        await business.on_deleted_business_messages(state.api, deleted_update(range(7, 13)))
        assert state.api.files == [], "пока окно не закрылось — ничего не шлём"
        await business.flush_pending()

    asyncio.run(scenario())
    assert len(state.api.files) == 1
    assert "**12**" in state.api.captions[0], "пачки сложились"


def test_split_updates_below_threshold_stay_cards():
    config.PURGE_DEBOUNCE_SEC = 5
    talk(4)
    state.api.sent.clear()

    async def scenario():
        await business.on_deleted_business_messages(state.api, deleted_update([1, 2]))
        await business.on_deleted_business_messages(state.api, deleted_update([3]))
        await business.flush_pending()

    asyncio.run(scenario())
    assert state.api.files == []
    assert sum("Удалённое сообщение" in text for text in state.api.texts) == 3


def test_own_deletions_are_not_counted():
    talk(12)
    state.api.sent.clear()
    for msg_id in range(1, 13):
        state.mark_own_deletion(OWNER, PEER, msg_id)
    purge(range(1, 13))
    assert state.api.sent == [] and state.api.files == []


def test_duplicate_ids_counted_once():
    config.PURGE_DEBOUNCE_SEC = 5
    talk(12)
    state.api.sent.clear()

    async def scenario():
        await business.on_deleted_business_messages(state.api, deleted_update(range(1, 13)))
        await business.on_deleted_business_messages(state.api, deleted_update(range(1, 13)))
        await business.flush_pending()

    asyncio.run(scenario())
    assert "**12**" in state.api.captions[0], "повтор апдейта не удваивает счёт"


# ------------------------------------------------------------- сборщик -----

def test_transcript_builder_stats():
    rows = [{"date": 100, "msg_id": 1, "user_id": 5, "user_name": "Вася",
             "text": "привет", "media_type": None},
            {"date": 200, "msg_id": 2, "user_id": OWNER, "user_name": "Я",
             "text": "", "media_type": "фото"}]
    payload, stats = transcript.build(rows, chat_title="Вася", owner_id=OWNER,
                                      requested=5, when=300)
    assert stats == {"requested": 5, "recovered": 2, "missing": 3, "media": 1,
                     "first": 100, "last": 200}
    text = payload.decode("utf-8")
    assert "привет" in text and "<фото>" in text and "Вы" in text


# ------------------------------------------- настоящее окно ожидания -------
# Все тесты выше выставляют PURGE_DEBOUNCE_SEC = 0 и обходят задачу ожидания
# стороной. Именно в ней жила ошибка: flush() отменял задачу, из которой сам
# же и вызывался, — CancelledError прилетал в него на первом await, и отчёт
# не уходил вообще. Эти тесты идут по боевому пути, с реальной паузой.

WINDOW = 0.05


def run_with_window(fill, *updates, extra_wait=0.2):
    async def scenario():
        config.PURGE_DEBOUNCE_SEC = WINDOW
        await fill()
        state.api.sent.clear()
        state.api.files.clear()
        for ids in updates:
            await business.on_deleted_business_messages(
                state.api, deleted_update(ids))
        await asyncio.sleep(WINDOW + extra_wait)
    asyncio.run(scenario())
    return state.api


async def fill_chat(count, start=1):
    for offset in range(count):
        await business.on_business_message(state.api, business_message(
            f"сообщение {start + offset}", message_id=start + offset,
            date=1700000000 + offset))


def test_single_deletion_survives_the_waiting_window():
    api = run_with_window(lambda: fill_chat(1), [1])
    assert sum("Удалённое сообщение" in text for text in api.texts) == 1
    assert len(asyncio.run(db.last_deleted(OWNER, PEER, 10))) == 1


def test_mass_deletion_survives_the_waiting_window():
    api = run_with_window(lambda: fill_chat(12), range(1, 13))
    assert len(api.files) == 1
    assert len(asyncio.run(db.last_deleted(OWNER, PEER, 100))) == 12


def test_two_updates_inside_the_window_produce_one_report():
    api = run_with_window(lambda: fill_chat(12), range(1, 7), range(7, 13))
    assert len(api.files) == 1
    assert "**12**" in api.captions[0]


def test_the_waiting_task_finishes_cleanly():
    """Задача ожидания не должна отменять сама себя."""
    async def scenario():
        config.PURGE_DEBOUNCE_SEC = WINDOW
        await fill_chat(1)
        await business.on_deleted_business_messages(state.api, deleted_update([1]))
        task = business._pending[(OWNER, PEER)].task
        await asyncio.sleep(WINDOW + 0.2)
        assert task.done() and not task.cancelled(), "оборвалась на полпути"
        assert business._pending == {}, "пачка разобрана и убрана"
    asyncio.run(scenario())


# ------------------------------------------------ ничего не теряется молча --
# Бот молчал, если удалённых сообщений не было в его памяти. Со стороны это
# неотличимо от поломки: удалили несколько — в ответ тишина.

def test_deletion_of_unknown_messages_is_still_reported():
    api = run_with_window(lambda: fill_chat(0), [1, 2])
    assert any("Удалено сообщений: 2" in text for text in api.texts)
    assert any("нет в моей памяти" in text for text in api.texts)


def test_single_unknown_deletion_is_reported_too():
    api = run_with_window(lambda: fill_chat(0), [1])
    assert any("Удалено сообщений: 1" in text for text in api.texts)


def test_partial_coverage_says_what_was_lost():
    api = run_with_window(lambda: fill_chat(1), [1, 2, 3])
    assert sum("Удалённое сообщение" in text for text in api.texts) == 1
    assert any("удалено ещё **2**" in text for text in api.texts)


def test_full_coverage_adds_no_note():
    api = run_with_window(lambda: fill_chat(2), [1, 2])
    assert sum("Удалённое сообщение" in text for text in api.texts) == 2
    assert not any("нет в моей памяти" in text for text in api.texts)


def test_the_report_names_the_storage_period():
    """Владельцу должно быть понятно, почему сообщений нет."""
    api = run_with_window(lambda: fill_chat(0), [1, 2])
    notice = [text for text in api.texts if "нет в моей памяти" in text][0]
    assert "30 дней" in notice


def test_one_broken_card_does_not_eat_the_others():
    calls = {"n": 0}
    original = business.reporter.send_report

    async def flaky(owner_id, text, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Telegram не принял")
        return await original(owner_id, text, **kwargs)

    business.reporter.send_report = flaky
    try:
        api = run_with_window(lambda: fill_chat(3), [1, 2, 3])
        assert sum("Удалённое сообщение" in text for text in api.texts) == 2
    finally:
        business.reporter.send_report = original


def test_cache_keeps_messages_long_enough_to_be_useful():
    """Двух суток мало: удаляют обычно не самое свежее."""
    assert config.CACHE_TTL_HOURS >= 24 * 7
