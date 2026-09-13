"""Mini App: подпись Telegram, доступ и действия страницы.

Здесь единственная защита — подпись initData. Всё остальное приходит из
браузера, то есть от кого угодно, поэтому тесты ниже про одно: владельца берём
из подписи и никогда из тела запроса.
"""
import asyncio
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

import config
import db
import web
from bot import webapp
from core import state
from tests.fakes import ALL_RIGHTS, FakeBotAPI, approve

TOKEN = "123456:AAHfake-token-for-tests"
OWNER, STRANGER, PEER = 111, 222, 777
BIZ = "biz1"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    from core import chatprefs

    config.DB_PATH.unlink(missing_ok=True)
    asyncio.run(db.init())
    chatprefs.invalidate_all()
    monkeypatch.setattr(config, "BOT_TOKEN", TOKEN)
    config.OWNER_ID = OWNER
    state.users.clear()
    state.business.clear()
    state.mutes.clear()
    state.allowlist.clear()
    approve(OWNER, OWNER)
    state.business[BIZ] = {"user_id": OWNER, "user_chat_id": OWNER,
                           "is_enabled": True, "rights": dict(ALL_RIGHTS)}
    state.api = FakeBotAPI()
    yield
    asyncio.run(db.close())
    state.api = None
    state.users.clear()
    state.business.clear()
    state.mutes.clear()
    config.OWNER_ID = 0


def sign(user_id=OWNER, *, token=TOKEN, age=0, extra=None, name="Тест") -> str:
    """Собирает initData ровно так, как это делает Telegram."""
    fields = {
        "auth_date": str(int(time.time()) - age),
        "query_id": "AAH_query",
        "user": json.dumps({"id": user_id, "first_name": name},
                           ensure_ascii=False, separators=(",", ":")),
    }
    fields.update(extra or {})
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


# ------------------------------------------------------------- подпись -----

def test_a_real_signature_passes():
    assert webapp.check(sign(), token=TOKEN)["id"] == OWNER


def test_a_forged_user_is_rejected():
    """Главное: подменить id в подписанной строке нельзя."""
    good = sign()
    forged = good.replace(str(OWNER), str(STRANGER))
    assert forged != good
    with pytest.raises(webapp.Denied):
        webapp.check(forged, token=TOKEN)


def test_a_signature_from_another_bot_is_rejected():
    with pytest.raises(webapp.Denied):
        webapp.check(sign(token="999:OTHER"), token=TOKEN)


def test_a_missing_or_empty_signature_is_rejected():
    for bad in ("", "user=%7B%22id%22%3A111%7D&auth_date=1", "hash="):
        with pytest.raises(webapp.Denied):
            webapp.check(bad, token=TOKEN)


def test_an_old_signature_is_rejected():
    """Подпись сама не протухает — украденная работала бы вечно."""
    webapp.check(sign(age=webapp.MAX_AGE - 60), token=TOKEN)
    with pytest.raises(webapp.Denied):
        webapp.check(sign(age=webapp.MAX_AGE + 60), token=TOKEN)


def test_extra_fields_are_covered_by_the_signature():
    """Telegram подписывает все поля: дописать своё к чужой подписи нельзя."""
    good = sign()
    with pytest.raises(webapp.Denied):
        webapp.check(good + "&chat_instance=подделка", token=TOKEN)


def test_a_stranger_with_a_valid_signature_is_still_refused():
    """Подпись говорит «это он», а не «ему можно»."""
    webapp.check(sign(STRANGER), token=TOKEN)
    with pytest.raises(webapp.Denied):
        asyncio.run(webapp.owner_of(sign(STRANGER)))


# ----------------------------------------------------------------- HTTP ----

class Request:
    def __init__(self, what, payload=None, init=None):
        self.match_info = {"what": what}
        self.headers = {web.INIT_HEADER: init} if init is not None else {}
        self.remote = "1.2.3.4"
        self._payload = payload
        self.can_read_body = payload is not None

    async def json(self):
        return self._payload


def call(what, payload=None, init=None):
    response = asyncio.run(web._api(Request(what, payload, init)))
    return response.status, json.loads(response.body.decode())


def test_the_api_refuses_an_unsigned_request():
    status, body = call("state", {}, init="")
    assert status == 403 and body == {"error": "denied"}


def test_the_api_does_not_explain_why_it_refused():
    """По разнице формулировок удобно подбирать подпись — ответ один на всё."""
    answers = {call("state", {}, init=bad)[1]["error"]
               for bad in ("", "hash=deadbeef", sign(token="999:OTHER"))}
    assert answers == {"denied"}


def test_the_api_answers_a_signed_request():
    status, body = call("state", {}, init=sign())
    assert status == 200
    assert body["connected"] is True and body["dnd"] is False
    assert body["stats"]["deleted"] == 0


def test_the_body_cannot_choose_whose_data_to_return():
    """Единственный источник owner_id — подпись."""
    asyncio.run(db.add_intercepted(
        {"owner_id": STRANGER, "chat_id": PEER, "msg_id": 1, "user_id": PEER,
         "user_name": "Чужой", "text": "секрет", "date": db.now()}, "mute"))
    approve(STRANGER, STRANGER)

    status, body = call("list", {"what": "intercepted", "owner_id": STRANGER},
                        init=sign(OWNER))
    assert status == 200 and body["items"] == []


def test_lists_come_back_as_rows():
    asyncio.run(db.add_intercepted(
        {"owner_id": OWNER, "chat_id": PEER, "msg_id": 1, "user_id": PEER,
         "user_name": "Вася", "text": "привет", "date": db.now()}, "mute"))
    status, body = call("list", {"what": "intercepted"}, init=sign())
    assert status == 200
    assert body["items"][0]["text"] == "привет"
    assert body["items"][0]["reason"] == "mute"


def test_an_unknown_list_is_a_bad_request():
    status, _ = call("list", {"what": "почта"}, init=sign())
    assert status == 400


def test_broken_json_does_not_reach_the_handlers():
    class Broken(Request):
        async def json(self):
            raise ValueError("не json")

    response = asyncio.run(web._api(Broken("state", {}, sign())))
    assert response.status == 400


# -------------------------------------------------------------- действия ---

def test_dnd_can_be_switched_from_the_page():
    status, body = call("act", {"action": "dnd", "on": True}, init=sign())
    assert status == 200 and body["dnd"] is True
    assert state.dnd_active(OWNER)

    status, body = call("act", {"action": "dnd", "on": False}, init=sign())
    assert body["dnd"] is False and not state.dnd_active(OWNER)


def test_switching_dnd_from_the_page_is_announced_in_the_chat():
    """Владелец закрыл приложение — в чате должно остаться, что режим включён."""
    call("act", {"action": "dnd", "on": True}, init=sign())
    said = state.api.texts_to(OWNER)
    assert said and "включён" in said[-1]
    assert "ungmute" in said[-1], "и как его выключить"


def test_turning_dnd_off_from_the_page_reports_and_sums_up():
    asyncio.run(state.set_dnd(OWNER))
    asyncio.run(db.add_intercepted(
        {"owner_id": OWNER, "chat_id": PEER, "msg_id": 1, "user_id": PEER,
         "user_name": "Вася", "text": "ты тут?", "date": db.now()}, "dnd"))
    state.api.sent.clear()

    call("act", {"action": "dnd", "on": False}, init=sign())
    assert any("выключен" in text for text in state.api.texts_to(OWNER))
    assert state.api.files, "и сводка «пока вас не беспокоили»"


def test_switching_dnd_to_the_same_state_says_nothing_twice():
    """Приложение могло переоткрыться — второе «включён» было бы враньём."""
    call("act", {"action": "dnd", "on": True}, init=sign())
    state.api.sent.clear()
    status, body = call("act", {"action": "dnd", "on": True}, init=sign())
    assert status == 200 and body["dnd"] is True
    assert state.api.texts_to(OWNER) == []


def test_mute_can_be_lifted_from_the_page():
    asyncio.run(state.mute_user(OWNER, PEER, PEER, 0))
    call("act", {"action": "unmute", "user_id": PEER, "chat_id": PEER},
         init=sign())
    assert not asyncio.run(state.is_muted(OWNER, PEER, PEER))


def test_the_allowlist_can_be_edited_from_the_page():
    call("act", {"action": "allow", "user_id": PEER}, init=sign())
    assert state.is_allowed(OWNER, PEER)
    call("act", {"action": "deny", "user_id": PEER}, init=sign())
    assert not state.is_allowed(OWNER, PEER)


def test_cleanup_works_and_touches_only_its_owner():
    for owner in (OWNER, STRANGER):
        asyncio.run(db.add_urgent_call(owner, PEER, "Вася", PEER, "срочно"))

    status, body = call("act", {"action": "clear", "kind": "urgent"}, init=sign())
    assert status == 200 and body["removed"] == 1
    assert asyncio.run(db.count_kind("urgent", OWNER)) == 0
    assert asyncio.run(db.count_kind("urgent", STRANGER)) == 1


def test_cleanup_refuses_a_table_name_it_was_not_given():
    for junk in ("messages", "users", "'; DROP TABLE messages; --"):
        status, _ = call("act", {"action": "clear", "kind": junk}, init=sign())
        assert status == 400, junk


def test_unknown_actions_and_settings_are_refused():
    assert call("act", {"action": "выключить-всё"}, init=sign())[0] == 400
    assert call("act", {"action": "chat", "chat_id": PEER,
                        "key": "owner_id"}, init=sign())[0] == 400


def test_a_non_numeric_id_is_a_bad_request():
    status, _ = call("act", {"action": "unmute", "user_id": "себя"}, init=sign())
    assert status == 400


# ---------------------------------------------------------------- кнопка ---

def test_the_menu_button_opens_the_app_when_there_is_a_public_url(monkeypatch):
    from bot import commands

    monkeypatch.setattr(config, "WEBHOOK_URL", "https://guard.example.com")
    asyncio.run(commands.publish_menu(state.api))
    button = state.api.menu_button
    assert button["type"] == "web_app"
    assert button["web_app"]["url"] == "https://guard.example.com/app"


def test_without_a_public_url_the_button_falls_back_to_commands(monkeypatch):
    """Иначе на кнопке осталась бы ссылка от прошлой версии."""
    from bot import commands

    monkeypatch.setattr(config, "WEBHOOK_URL", "")
    asyncio.run(commands.publish_menu(state.api))
    assert state.api.menu_button == {"type": "commands"}


def test_plain_http_is_not_offered_to_telegram(monkeypatch):
    """Telegram открывает Mini App только по https."""
    monkeypatch.setattr(config, "WEBHOOK_URL", "http://guard.example.com")
    assert config.webapp_url() == ""


def test_the_app_can_be_turned_off(monkeypatch):
    monkeypatch.setattr(config, "WEBHOOK_URL", "https://guard.example.com")
    monkeypatch.setattr(config, "WEBAPP", False)
    assert config.webapp_url() == ""


# ---------------------------------------------------------------- страница -

def test_the_page_is_served_and_looks_like_the_app():
    body = asyncio.run(web._page(Request("page"))).text
    assert "telegram-web-app.js" in body, "без него нет ни темы, ни initData"
    assert "X-Init-Data" in body, "страница обязана подписываться"
    assert "/app/api/" in body


class Asset:
    def __init__(self, name):
        self.match_info = {"name": name}


def test_the_logo_is_served():
    response = asyncio.run(web._asset(Asset("logo.svg")))
    assert response.status == 200
    assert response.content_type == "image/svg+xml"
    assert b"<svg" in response.body


@pytest.mark.parametrize("name", [
    "нет.svg", "../config.py", "index.html", "../../etc/passwd",
])
def test_only_listed_assets_are_served(name):
    """Раздаём поимённо: каталог целиком наружу отдавать незачем."""
    assert asyncio.run(web._asset(Asset(name))).status == 404


def test_assets_are_linked_absolutely():
    """Страница живёт на /app без слеша: относительный путь уедет в корень."""
    body = asyncio.run(web._page(Request("page"))).text
    assert 'src="/app/logo.svg"' in body
    assert 'src="logo.svg"' not in body


def test_the_page_avoids_css_the_telegram_webview_may_not_know():
    """color-mix есть не везде: незакрашенный фон выглядит как поломка."""
    body = asyncio.run(web._page(Request("page"))).text
    # Со скобкой: само слово встречается в комментарии, объясняющем запрет.
    assert "color-mix(" not in body


def test_tabs_do_not_need_horizontal_scrolling():
    """Лента обрезалась по краям экрана — вкладки разложены сеткой."""
    body = asyncio.run(web._page(Request("page"))).text
    tabs = body[body.index(".tabs {"):body.index(".tab {")]
    assert "grid" in tabs and "overflow-x" not in tabs


def test_clear_all_is_accepted_from_the_page():
    """Кнопка «Всё сразу» шлёт особый ключ — он должен проходить проверку."""
    asyncio.run(db.add_urgent_call(OWNER, PEER, "Вася", PEER, "срочно"))
    asyncio.run(db.add_intercepted(
        {"owner_id": OWNER, "chat_id": PEER, "msg_id": 1, "user_id": PEER,
         "user_name": "Вася", "text": "x", "date": db.now()}, "mute"))

    status, body = call("act", {"action": "clear", "kind": "*"}, init=sign())
    assert status == 200 and body["removed"] == 2
    assert not any(asyncio.run(db.counts_by_kind(OWNER)).values())


def test_the_page_never_offers_more_than_the_api_allows():
    """Список областей страница берёт с сервера, а не выдумывает."""
    status, body = call("state", {}, init=sign())
    assert status == 200
    assert {kind["key"] for kind in body["kinds"]} <= set(db.PURGEABLE)


# ----------------------------------------------- запасной вход в приложение -

def start(monkeypatch, url="https://guard.example.com"):
    from bot import commands

    monkeypatch.setattr(config, "WEBHOOK_URL", url)
    asyncio.run(commands.handle(state.api, {
        "message_id": 1, "date": int(time.time()),
        "chat": {"id": OWNER, "type": "private"},
        "from": {"id": OWNER, "first_name": "Я"}, "text": "/start"}))
    return state.api


def test_start_carries_a_button_that_the_client_cannot_cache(monkeypatch):
    """Синюю кнопку клиент кэширует, кнопку в сообщении — нет."""
    api = start(monkeypatch)
    button = api.markups[-1]["inline_keyboard"][0][0]
    assert button["web_app"]["url"] == "https://guard.example.com/app"


def test_start_reapplies_the_menu_button(monkeypatch):
    """Если при старте вызов не прошёл, другого повода повторить его нет."""
    api = start(monkeypatch)
    assert api.menu_button["type"] == "web_app"


def test_without_an_app_there_is_no_button_at_all(monkeypatch):
    api = start(monkeypatch, url="")
    assert api.markups[-1] is None


def test_status_says_why_the_app_is_unavailable(monkeypatch):
    from bot import commands

    monkeypatch.setattr(config, "WEBHOOK_URL", "http://insecure.example.com")
    asyncio.run(commands.handle(state.api, {
        "message_id": 2, "date": int(time.time()),
        "chat": {"id": OWNER, "type": "private"},
        "from": {"id": OWNER, "first_name": "Я"}, "text": "/status"}))
    assert "не https" in state.api.texts[-1]


@pytest.mark.parametrize("url,why", [
    ("https://ok.example.com", ""),
    ("", "нет публичного адреса"),
    ("http://ok.example.com", "не https"),
])
def test_the_reason_is_named_precisely(monkeypatch, url, why):
    monkeypatch.setattr(config, "WEBHOOK_URL", url)
    assert why in config.webapp_why()
