"""Тесты на подключение проверенного спека Авито.

Отдельно от test_avito.py: там транспорт (токен, ретраи, DRY_RUN), здесь —
то, что появилось вместе со спекой: обрезка текста под лимит, двухшаговая
отправка фото, секрет в пути вебхука и вычистка секретов из логов.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.webhooks as webhooks_module
from app.channels import avito_endpoints as ep
from app.channels import avito_payloads as pl
from app.channels.avito import AvitoAuth, AvitoClient, truncate_for_avito
from app.config import Settings
from app.logging_setup import redact
from tests.test_avito import WEBHOOK_SECRET, FakeRedis, _url, send_url, token_url


@pytest.fixture
def settings():
    return Settings(
        avito_client_id="test_id",
        avito_client_secret="test_secret",
        avito_user_id="777",
        avito_webhook_secret=WEBHOOK_SECRET,
        dry_run=False,
        avito_max_retries=3,
    )


def _client(settings) -> AvitoClient:
    return AvitoClient(settings=settings, auth=AvitoAuth(settings, redis=FakeRedis()))


def _mock_token() -> None:
    respx.post(token_url()).mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
    )


# --------------------------------------------------------------------------
# Обрезка текста до лимита Авито
# --------------------------------------------------------------------------

def test_short_text_is_untouched():
    assert truncate_for_avito("Здравствуйте!") == "Здравствуйте!"


def test_text_at_exact_limit_is_untouched():
    text = "я" * ep.MESSAGE_TEXT_MAX_LENGTH
    assert truncate_for_avito(text) == text


def test_long_text_is_truncated_within_limit():
    result = truncate_for_avito("слово " * 400)
    assert len(result) <= ep.MESSAGE_TEXT_MAX_LENGTH
    assert result.endswith("…")


def test_truncation_prefers_word_boundary():
    result = truncate_for_avito("длинноеслово " * 200)
    assert not result[:-1].endswith("длинноесло")


@respx.mock
async def test_send_message_truncates_and_warns(settings, caplog):
    """Агент целится в 700 символов, но полагаться на это нельзя: превышение
    лимита означает отказ доставки, то есть молчание в ответ клиенту."""
    _mock_token()
    captured: dict = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "m1"})

    respx.post(send_url()).mock(side_effect=handler)

    client = _client(settings)
    with caplog.at_level("WARNING"):
        await client.send_message("c1", "текст " * 400)

    assert len(captured["message"]["text"]) <= ep.MESSAGE_TEXT_MAX_LENGTH
    assert any("truncated" in r.getMessage() for r in caplog.records)
    await client.aclose()


@respx.mock
async def test_send_message_body_shape_matches_spec(settings):
    _mock_token()
    captured: dict = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "m1"})

    respx.post(send_url()).mock(side_effect=handler)

    client = _client(settings)
    await client.send_message("c1", "привет")
    assert captured == {"message": {"text": "привет"}, "type": "text"}
    await client.aclose()


# --------------------------------------------------------------------------
# Фото: два шага
# --------------------------------------------------------------------------

@respx.mock
async def test_upload_image_uses_bracketed_field_name(settings):
    """Поле называется ровно uploadfile[] — без скобок файл не примут."""
    _mock_token()
    seen: dict = {}

    def handler(request):
        seen["body"] = request.content.decode("utf-8", errors="replace")
        return httpx.Response(200, json={"img-1": {"1280x960": "https://example/x.jpg"}})

    respx.post(_url(ep.UPLOAD_IMAGES, user_id="777")).mock(side_effect=handler)

    client = _client(settings)
    await client.upload_image(b"jpegbytes", "photo.jpg")

    assert 'name="uploadfile[]"' in seen["body"]
    await client.aclose()


@respx.mock
async def test_upload_and_send_image_is_two_steps(settings):
    _mock_token()
    upload = respx.post(_url(ep.UPLOAD_IMAGES, user_id="777")).mock(
        return_value=httpx.Response(200, json={"img-42": {"640x480": "https://example/x.jpg"}})
    )
    sent: dict = {}

    def send_handler(request):
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "m2"})

    send = respx.post(_url(ep.SEND_IMAGE, user_id="777", chat_id="c1")).mock(
        side_effect=send_handler
    )

    client = _client(settings)
    await client.upload_and_send_image("c1", b"bytes")

    assert upload.call_count == 1 and send.call_count == 1
    assert sent == {"image_id": "img-42"}
    await client.aclose()


# --------------------------------------------------------------------------
# Секрет в пути вебхука (вместо неопубликованной подписи)
# --------------------------------------------------------------------------

def _app(settings, monkeypatch) -> TestClient:
    monkeypatch.setattr(webhooks_module, "get_settings", lambda: settings)
    application = FastAPI()
    application.include_router(webhooks_module.router)
    return TestClient(application)


def test_webhook_accepts_correct_secret(settings, monkeypatch):
    client = _app(settings, monkeypatch)
    assert client.post(f"/webhook/avito/{WEBHOOK_SECRET}", json={"id": "m1"}).status_code == 200


def test_webhook_wrong_secret_returns_404(settings, monkeypatch):
    """404, а не 403: сканирующему не подтверждаем, что маршрут существует."""
    client = _app(settings, monkeypatch)
    assert client.post("/webhook/avito/" + "y" * 40, json={"id": "m1"}).status_code == 404


def test_webhook_without_secret_segment_returns_404(settings, monkeypatch):
    client = _app(settings, monkeypatch)
    assert client.post("/webhook/avito", json={"id": "m1"}).status_code == 404


def test_empty_configured_secret_rejects_everything(monkeypatch):
    """Незаполненный секрет не должен означать «пускать всех»."""
    client = _app(Settings(avito_webhook_secret=""), monkeypatch)
    assert client.post("/webhook/avito/anything", json={"id": "m1"}).status_code == 404


def test_registered_path_matches_served_route(settings, monkeypatch):
    """Путь, который регистрирует scripts/register_webhook.py, обязан
    совпадать с тем, что реально обслуживает приложение."""
    monkeypatch.setattr(webhooks_module, "get_settings", lambda: settings)
    path = webhooks_module.webhook_path(WEBHOOK_SECRET)
    assert _app(settings, monkeypatch).post(path, json={"id": "m1"}).status_code == 200


def test_webhook_secret_must_be_long_enough():
    with pytest.raises(RuntimeError, match="AVITO_WEBHOOK_SECRET"):
        Settings(avito_webhook_secret="short").require_webhook_secret()
    assert Settings(avito_webhook_secret=WEBHOOK_SECRET).require_webhook_secret() == WEBHOOK_SECRET


# --------------------------------------------------------------------------
# Гигиена логов и разбор payload
# --------------------------------------------------------------------------

def test_redact_scrubs_query_string_secrets():
    url = (
        "https://api.avito.ru/token?grant_type=client_credentials"
        "&client_id=abc&client_secret=s3cr3t"
    )
    cleaned = redact(url)
    assert "s3cr3t" not in cleaned
    assert "abc" not in cleaned
    assert "grant_type=client_credentials" in cleaned


def test_echo_of_our_own_message_is_detected():
    """Без этой проверки агент отвечал бы на собственные сообщения."""
    payload = {"payload": {"value": {"author_id": "777", "chat_id": "c1"}}}
    assert pl.is_outgoing_echo(payload, "777") is True
    assert pl.is_outgoing_echo(payload, "999") is False


def test_text_extraction_tolerates_envelope_shapes():
    assert pl.extract_text({"payload": {"value": {"content": {"text": "привет"}}}}) == "привет"
    assert pl.extract_text({"payload": {"value": {"text": "hi"}}}) == "hi"
    assert pl.extract_text({"junk": 1}) is None
