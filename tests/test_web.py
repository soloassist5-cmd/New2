"""HTTP-сервер: healthcheck и приём вебхука."""
import asyncio
import json

import pytest
from aiohttp import web as aioweb

import config
import web
from core import state
from tests.fakes import FakeBotAPI


class FakeRequest:
    def __init__(self, payload, secret=None, remote="1.2.3.4"):
        self.headers = {web.SECRET_HEADER: secret} if secret is not None else {}
        self._payload = payload
        self.remote = remote

    async def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return json.loads(self._payload) if isinstance(self._payload, str) \
            else self._payload


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(config, "BOT_TOKEN", "123:ABC")
    monkeypatch.setattr(config, "WEBHOOK_URL", "https://guard.example.com")
    monkeypatch.setattr(config, "WEBHOOK_SECRET", "s3cret")
    state.api = FakeBotAPI()
    yield
    state.api = None


def post(request):
    return asyncio.run(web._webhook(request))


def test_path_hides_both_secret_and_token():
    """URL попадает в логи и прокси, поэтому в нём не должно быть секретов."""
    path = config.webhook_path()
    assert path.startswith("/tg/")
    assert config.webhook_secret() not in path
    assert config.BOT_TOKEN not in path and "ABC" not in path
    assert path == config.webhook_path(), "путь стабилен между вызовами"


def test_path_changes_with_the_token(monkeypatch):
    first = config.webhook_path()
    monkeypatch.setattr(config, "BOT_TOKEN", "999:XYZ")
    assert config.webhook_path() != first


def test_secret_is_derived_when_not_configured(monkeypatch):
    monkeypatch.setattr(config, "WEBHOOK_SECRET", "")
    assert len(config.webhook_secret()) == 32
    assert config.BOT_TOKEN not in config.webhook_secret()


def test_update_reaches_the_dispatcher(monkeypatch):
    seen = []

    async def fake_dispatch(api, update):
        seen.append(update)

    monkeypatch.setattr(web.poller, "dispatch", fake_dispatch)
    response = post(FakeRequest({"update_id": 1, "message": {}}, secret="s3cret"))
    assert response.status == 200
    assert seen == [{"update_id": 1, "message": {}}]


def test_wrong_secret_is_rejected(monkeypatch):
    seen = []

    async def fake_dispatch(api, update):
        seen.append(update)

    monkeypatch.setattr(web.poller, "dispatch", fake_dispatch)
    response = post(FakeRequest({"update_id": 1}, secret="чужой"))
    assert response.status == 403
    assert seen == [], "тело даже не разбираем"


def test_missing_secret_header_is_rejected():
    assert post(FakeRequest({"update_id": 1})).status == 403


def test_broken_json_gets_400():
    assert post(FakeRequest(ValueError("bad"), secret="s3cret")).status == 400


def test_handler_error_still_answers_200(monkeypatch):
    """Ошибка не должна заставлять Telegram слать апдейт по кругу."""
    async def boom(api, update):
        raise RuntimeError("сломалось")

    monkeypatch.setattr(web.poller, "dispatch", boom)
    assert post(FakeRequest({"update_id": 1}, secret="s3cret")).status == 200


def test_health_reports_transport():
    response = asyncio.run(web._status(None))
    assert isinstance(response, aioweb.Response)
    body = json.loads(response.body)
    assert body["transport"] == "webhook"
    assert body["bot"] is True


def test_polling_mode_when_no_public_url(monkeypatch):
    monkeypatch.setattr(config, "WEBHOOK_URL", "")
    assert config.use_webhook() is False
    body = json.loads(asyncio.run(web._status(None)).body)
    assert body["transport"] == "polling"
