"""Тесты транспортного слоя адаптера Авито.

Пути берутся из `avito_endpoints` (проверенный OpenAPI-спек), а не хардкодятся
в тестах: если спек уточнят, тесты продолжат проверять поведение, а не строки.

Реальных ключей нигде нет — фикстура собирает одноразовый Settings.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.channels import avito_endpoints as ep
from app.channels import avito_payloads as pl
from app.channels.avito import (
    AvitoAuth,
    AvitoClient,
    AvitoTokenError,
    SpecNotVerifiedError,
    _is_retryable,
    truncate_for_avito,
)
from app.config import Settings
from app.channels import inbound_dedup as dedup

# asyncio_mode=auto in pytest.ini handles the async tests; a module-level
# marker would also (wrongly) tag the synchronous ones.

WEBHOOK_SECRET = "x" * 40


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

class FakeRedis:
    """Enough of redis-py's async surface for these tests."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def ttl(self, key):
        return self.ttls.get(key, -1)

    async def delete(self, key):
        self.store.pop(key, None)
        self.ttls.pop(key, None)


@pytest.fixture
def settings():
    return Settings(
        avito_client_id="test_id",
        avito_client_secret="test_secret",
        avito_user_id="777",
        avito_webhook_secret=WEBHOOK_SECRET,
        dry_run=False,
        avito_max_retries=3,
        database_url="postgresql+asyncpg://x/y",
    )


def _url(spec: tuple[str, str], **kwargs) -> str:
    return ep.BASE_URL + spec[1].format(**kwargs)


def token_url() -> str:
    return _url(ep.TOKEN)


def messages_url(user_id="777", chat_id="c1") -> str:
    return _url(ep.GET_MESSAGES, user_id=user_id, chat_id=chat_id)


def send_url(user_id="777", chat_id="c1") -> str:
    return _url(ep.SEND_MESSAGE, user_id=user_id, chat_id=chat_id)


# --------------------------------------------------------------------------
# Спек и его особенности
# --------------------------------------------------------------------------

def test_spec_is_marked_verified():
    assert ep.SPEC_VERIFIED is True


def test_get_messages_path_keeps_trailing_slash():
    """Без завершающего слеша этот эндпоинт отдаёт 404."""
    assert ep.GET_MESSAGES[1].endswith("/messages/")


def test_upload_field_name_has_brackets():
    assert ep.UPLOAD_IMAGES_FIELD == "uploadfile[]"


def test_signature_algorithm_is_still_unknown():
    """Если кто-то «починит» это, добавив угаданный HMAC, тест должен
    привлечь внимание к комментарию в avito_endpoints.py."""
    assert ep.WEBHOOK_SIGNATURE_ALGORITHM_KNOWN is False


async def test_client_refuses_to_call_when_spec_flag_is_off(settings, monkeypatch):
    """Страховка: если константы когда-нибудь снова станут непроверенными,
    исходящие запросы должны блокироваться, а не идти наугад."""
    monkeypatch.setattr(ep, "SPEC_VERIFIED", False)
    client = AvitoClient(settings=settings)
    with pytest.raises(SpecNotVerifiedError):
        await client.list_chats()
    await client.aclose()


# --------------------------------------------------------------------------
# Token lifecycle
# --------------------------------------------------------------------------

@respx.mock
async def test_token_is_fetched_and_cached_in_redis(settings):
    redis = FakeRedis()
    respx.post(token_url()).mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )
    auth = AvitoAuth(settings, redis=redis)

    assert await auth.get_token() == "tok-1"
    # TTL is expires_in minus the safety margin, so no request straddles expiry.
    assert redis.ttls["avito:access_token"] == 3600 - settings.token_expiry_safety_margin_seconds


@respx.mock
async def test_token_is_not_refetched_while_valid(settings):
    route = respx.post(token_url()).mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )
    auth = AvitoAuth(settings, redis=FakeRedis())
    await auth.get_token()
    await auth.get_token()
    assert route.call_count == 1


# --------------------------------------------------------------------------
# Отказ в выдаче токена приходит с HTTP 200
#
# Живой случай 2026-08-30: ключи в окружении оказались плейсхолдерами, Авито
# ответил `unauthorized_client` — и всё это под кодом 200. raise_for_status
# такое пропускает, и наружу вылезал `KeyError: 'access_token'`: трассировка
# без единого слова о причине, хотя Авито назвал её прямо в теле.
# --------------------------------------------------------------------------

@respx.mock
async def test_a_rejected_token_request_raises_a_named_error_not_a_key_error(settings):
    respx.post(token_url()).mock(
        return_value=httpx.Response(
            200,
            json={
                "error": "unauthorized_client",
                "error_description": (
                    "The client is not authorized to request a token using this method."
                ),
            },
        )
    )
    auth = AvitoAuth(settings, redis=FakeRedis())

    with pytest.raises(AvitoTokenError) as excinfo:
        await auth.get_token()

    message = str(excinfo.value)
    assert "unauthorized_client" in message
    assert "not authorized" in message


@respx.mock
async def test_the_rejection_reason_reaches_the_log(settings, caplog):
    """Причина обязана быть в логе целиком: по ней различаются «ключи не те»,
    «приложению не разрешён этот grant» и «интеграция не активирована»."""
    respx.post(token_url()).mock(
        return_value=httpx.Response(
            200,
            json={"error": "invalid_client", "error_description": "Client not found"},
        )
    )
    auth = AvitoAuth(settings, redis=FakeRedis())

    with caplog.at_level("ERROR", logger="parmangal.avito"):
        with pytest.raises(AvitoTokenError):
            await auth.get_token()

    assert "invalid_client" in caplog.text
    assert "Client not found" in caplog.text


@respx.mock
async def test_a_body_without_any_error_fields_still_fails_clearly(settings):
    """Ответ 200 без access_token и без error — тоже отказ, а не повод
    падать с KeyError на неожиданной форме."""
    respx.post(token_url()).mock(return_value=httpx.Response(200, json={"foo": "bar"}))
    auth = AvitoAuth(settings, redis=FakeRedis())

    with pytest.raises(AvitoTokenError) as excinfo:
        await auth.get_token()

    assert "access_token" in str(excinfo.value)


@respx.mock
async def test_a_rejected_token_is_not_cached(settings):
    """Иначе отказ осел бы в Redis и повторялся до истечения TTL."""
    redis = FakeRedis()
    respx.post(token_url()).mock(
        return_value=httpx.Response(200, json={"error": "unauthorized_client"})
    )
    auth = AvitoAuth(settings, redis=redis)

    with pytest.raises(AvitoTokenError):
        await auth.get_token()

    assert redis.store.get("avito:access_token") is None


@respx.mock
async def test_a_good_token_response_still_works(settings):
    """Проверка отказа не должна мешать нормальному пути."""
    respx.post(token_url()).mock(
        return_value=httpx.Response(200, json={"access_token": "tok-ok", "expires_in": 3600})
    )
    auth = AvitoAuth(settings, redis=FakeRedis())

    assert await auth.get_token() == "tok-ok"


@respx.mock
async def test_401_triggers_refresh_and_retry(settings):
    """The behaviour that keeps a rotated token from dropping a message."""
    tokens = iter(["stale", "fresh"])
    respx.post(token_url()).mock(
        side_effect=lambda req: httpx.Response(
            200, json={"access_token": next(tokens), "expires_in": 3600}
        )
    )

    calls: list[str] = []

    def messages_handler(request):
        calls.append(request.headers["Authorization"])
        if len(calls) == 1:
            return httpx.Response(401, json={"error": "expired"})
        return httpx.Response(200, json={"messages": []})

    respx.get(messages_url()).mock(side_effect=messages_handler)

    client = AvitoClient(settings=settings, auth=AvitoAuth(settings, redis=FakeRedis()))
    result = await client.get_messages("c1")

    assert result == {"messages": []}
    assert calls == ["Bearer stale", "Bearer fresh"]
    await client.aclose()


@respx.mock
async def test_token_never_appears_in_logs(settings, caplog):
    respx.post(token_url()).mock(
        return_value=httpx.Response(200, json={"access_token": "super-secret", "expires_in": 3600})
    )
    with caplog.at_level("DEBUG"):
        await AvitoAuth(settings, redis=FakeRedis()).get_token()

    blob = "\n".join(r.getMessage() + str(getattr(r, "__dict__", "")) for r in caplog.records)
    assert "super-secret" not in blob
    assert "test_secret" not in blob


# --------------------------------------------------------------------------
# Retries
# --------------------------------------------------------------------------

def test_retry_predicate_covers_5xx_and_transport_only():
    request = httpx.Request("GET", "https://api.avito.ru/x")

    assert _is_retryable(httpx.ConnectTimeout("t", request=request))
    assert _is_retryable(httpx.ConnectError("boom", request=request))
    assert _is_retryable(
        httpx.HTTPStatusError("500", request=request, response=httpx.Response(500, request=request))
    )
    # A 4xx is our bug — retrying only multiplies it.
    assert not _is_retryable(
        httpx.HTTPStatusError("400", request=request, response=httpx.Response(400, request=request))
    )
    assert not _is_retryable(
        httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))
    )
    assert not _is_retryable(ValueError("unrelated"))


@respx.mock
async def test_retries_on_500_then_succeeds(settings):
    respx.post(token_url()).mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
    )
    responses = [httpx.Response(500), httpx.Response(500), httpx.Response(200, json={"ok": True})]
    route = respx.get(messages_url()).mock(side_effect=responses)

    client = AvitoClient(settings=settings, auth=AvitoAuth(settings, redis=FakeRedis()))
    assert await client.get_messages("c1") == {"ok": True}
    assert route.call_count == 3
    await client.aclose()


@respx.mock
async def test_gives_up_after_max_retries(settings):
    respx.post(token_url()).mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
    )
    route = respx.get(messages_url()).mock(return_value=httpx.Response(503))

    client = AvitoClient(settings=settings, auth=AvitoAuth(settings, redis=FakeRedis()))
    with pytest.raises(httpx.HTTPStatusError):
        await client.get_messages("c1")
    assert route.call_count == settings.avito_max_retries
    await client.aclose()


@respx.mock
async def test_does_not_retry_4xx(settings):
    respx.post(token_url()).mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
    )
    route = respx.get(messages_url()).mock(return_value=httpx.Response(400))

    client = AvitoClient(settings=settings, auth=AvitoAuth(settings, redis=FakeRedis()))
    with pytest.raises(httpx.HTTPStatusError):
        await client.get_messages("c1")
    assert route.call_count == 1
    await client.aclose()


# --------------------------------------------------------------------------
# DRY_RUN
# --------------------------------------------------------------------------

@respx.mock
async def test_dry_run_does_not_send_message():
    """The safety property this whole mode exists for."""
    settings = Settings(
        avito_client_id="i", avito_client_secret="s", avito_user_id="777", dry_run=True
    )
    route = respx.post(send_url()).mock(return_value=httpx.Response(200, json={"id": "m1"}))

    client = AvitoClient(settings=settings, auth=AvitoAuth(settings, redis=FakeRedis()))
    result = await client.send_message("c1", "текст клиенту")

    assert result["dry_run"] is True
    assert route.call_count == 0, "в DRY_RUN сообщение не должно уходить в Авито"
    await client.aclose()


@respx.mock
async def test_dry_run_does_not_send_image():
    settings = Settings(
        avito_client_id="i", avito_client_secret="s", avito_user_id="777", dry_run=True
    )
    url = _url(ep.SEND_IMAGE, user_id="777", chat_id="c1")
    route = respx.post(url).mock(return_value=httpx.Response(200, json={}))

    client = AvitoClient(settings=settings, auth=AvitoAuth(settings, redis=FakeRedis()))
    assert (await client.send_image("c1", "img-1"))["dry_run"] is True
    assert route.call_count == 0
    await client.aclose()


@respx.mock
async def test_dry_run_off_actually_sends(settings):
    respx.post(token_url()).mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
    )
    route = respx.post(send_url()).mock(return_value=httpx.Response(200, json={"id": "m1"}))

    client = AvitoClient(settings=settings, auth=AvitoAuth(settings, redis=FakeRedis()))
    assert await client.send_message("c1", "привет") == {"id": "m1"}
    assert route.call_count == 1
    await client.aclose()


# --------------------------------------------------------------------------
# Идемпотентность входящих
#
# Живёт в app/channels/inbound_dedup.py, а не в app/webhooks.py: каналов
# приёма два (вебхук и поллер), и разводить их обязана одна общая точка.
# --------------------------------------------------------------------------

async def test_duplicate_message_id_is_dropped():
    redis = FakeRedis()
    assert await dedup.claim("m-1", redis) is True    # первая доставка
    assert await dedup.claim("m-1", redis) is False   # Авито повторило


async def test_distinct_message_ids_both_pass():
    redis = FakeRedis()
    assert await dedup.claim("m-1", redis) is True
    assert await dedup.claim("m-2", redis) is True


async def test_missing_message_id_is_not_treated_as_duplicate():
    """Нечем дедуплицировать — не то же, что дубль: страхует уникальный
    индекс в БД. Обратное означало бы, что сообщение без идентификатора
    молча исчезает."""
    assert await dedup.claim(None, FakeRedis()) is True


async def test_claim_is_provisional_until_confirmed():
    """Заявка живёт минуту, а не сутки, — иначе упавшая обработка теряет
    сообщение навсегда: ключ стоит, курсор поллера не двинулся, следующий
    проход молча отбросит его как дубль."""
    redis = FakeRedis()
    await dedup.claim("m-1", redis)
    key = "avito:seen_message:m-1"
    assert redis.ttls[key] == dedup.PROVISIONAL_TTL_SECONDS

    await dedup.confirm("m-1", redis, Settings().webhook_idempotency_ttl_seconds)
    assert redis.ttls[key] == Settings().webhook_idempotency_ttl_seconds


async def test_release_returns_the_message_to_circulation():
    redis = FakeRedis()
    assert await dedup.claim("m-1", redis) is True
    await dedup.release("m-1", redis)
    # Обработка упала, заявка снята — следующий проход обязан снова взять
    # это сообщение, а не отбросить его как уже виденное.
    assert await dedup.claim("m-1", redis) is True


def test_message_id_extraction_tolerates_envelope_shapes():
    assert pl.extract_message_id({"payload": {"value": {"id": "a"}}}) == "a"
    assert pl.extract_message_id({"payload": {"id": "b"}}) == "b"
    assert pl.extract_message_id({"id": 42}) == "42"
    assert pl.extract_message_id({"nothing": "here"}) is None


def test_chat_id_extraction_tolerates_envelope_shapes():
    assert pl.extract_chat_id({"payload": {"value": {"chat_id": "c"}}}) == "c"
    assert pl.extract_chat_id({"payload": {"value": {"chatId": "c2"}}}) == "c2"
    assert pl.extract_chat_id({"junk": 1}) is None


# --------------------------------------------------------------------------
# Тип сообщения и диагностика вебхука без текста
# --------------------------------------------------------------------------

def test_chat_type_extraction_tolerates_envelope_shapes():
    assert pl.extract_chat_type({"payload": {"value": {"chat_type": "u2i"}}}) == "u2i"
    assert pl.extract_chat_type({"value": {"chat_type": "u2u"}}) == "u2u"
    assert pl.extract_chat_type({"junk": 1}) is None


def test_item_id_from_chat_reads_the_item_context():
    """Спек (components/schemas/Chat): context.type == "item",
    context.value.id — ID объявления, число."""
    chat = {"id": "c1", "context": {"type": "item", "value": {"id": 1768287444}}}
    assert pl.extract_item_id_from_chat(chat) == "1768287444"


def test_item_id_from_chat_ignores_a_non_item_context():
    """Чат по профилю: в value.id лежит идентификатор НЕ объявления, и
    принять его за item_id значило бы отвечать по чужому объявлению."""
    chat = {"id": "c1", "context": {"type": "user", "value": {"id": 42}}}
    assert pl.extract_item_id_from_chat(chat) is None


def test_item_id_from_chat_handles_missing_pieces():
    assert pl.extract_item_id_from_chat({}) is None
    assert pl.extract_item_id_from_chat({"context": {}}) is None
    assert pl.extract_item_id_from_chat({"context": {"type": "item"}}) is None
    assert pl.extract_item_id_from_chat({"context": {"type": "item", "value": {}}}) is None
    assert pl.extract_item_id_from_chat("не словарь") is None


def test_message_type_extraction_tolerates_envelope_shapes():
    assert pl.extract_message_type({"payload": {"value": {"type": "image"}}}) == "image"
    assert pl.extract_message_type({"value": {"type": "call"}}) == "call"
    assert pl.extract_message_type({"junk": 1}) is None


def test_is_image_message_true_on_explicit_type():
    payload = {"payload": {"value": {"type": "image", "content": {}}}}
    assert pl.is_image_message(payload) is True


def test_is_image_message_falls_back_to_content_structure():
    """Тип не пришёл (или назван иначе, чем мы предположили) — content с
    ключом "image" всё равно достаточно, чтобы не принять фото за
    нераспознанное системное событие."""
    payload = {"payload": {"value": {"content": {"image": {"sizes": {}}}}}}
    assert pl.is_image_message(payload) is True


def test_is_image_message_false_for_text():
    payload = {"payload": {"value": {"type": "text", "content": {"text": "привет"}}}}
    assert pl.is_image_message(payload) is False


def test_is_image_message_false_for_unrelated_type():
    payload = {"payload": {"value": {"type": "call_missed"}}}
    assert pl.is_image_message(payload) is False


def test_describe_payload_reports_type_and_top_level_keys():
    payload = {"payload": {"value": {"type": "image", "content": {"image": {}}}}}
    description = pl.describe_payload_for_logging(payload)
    assert description["message_type"] == "image"
    assert description["top_level_keys"] == ["payload"]


def test_describe_payload_masks_phone_regardless_of_key_name():
    """Реальная форма нетекстовых вебхуков не подтверждена — телефон может
    оказаться под именем поля, которое мы не предусмотрели. Маскировка по
    значению (не только по ключу) — единственная гарантия."""
    payload = {"payload": {"value": {"type": "call", "unexpected_field": "+7 999 123-45-67"}}}
    description = pl.describe_payload_for_logging(payload)
    dumped = str(description["sanitized"])
    assert "999" not in dumped
    assert "123-45-67" not in dumped


def test_describe_payload_masks_underscore_prefixed_name_fields():
    """author_name/buyer_name — реальные имена полей в этом проекте (см.
    app/db/models.py:Chat.buyer_name). \\b на границе "_" не сработал бы —
    маскировка по ключу здесь ищет подстроку, а не целое слово."""
    payload = {"payload": {"value": {"author_name": "Иван Иванов"}}}
    description = pl.describe_payload_for_logging(payload)
    assert description["sanitized"]["payload"]["value"]["author_name"] == "***"


def test_describe_payload_preserves_deep_image_urls():
    """Реальная глубина фото-контента (content.image.sizes."WxH") не
    должна срезаться предохранителем от патологической вложенности."""
    payload = {
        "payload": {"value": {"type": "image", "content": {
            "image": {"sizes": {"140x105": "https://example.com/a.jpg"}}
        }}}
    }
    description = pl.describe_payload_for_logging(payload)
    sizes = description["sanitized"]["payload"]["value"]["content"]["image"]["sizes"]
    assert sizes == {"140x105": "https://example.com/a.jpg"}


# --------------------------------------------------------------------------
# Settings hygiene
# --------------------------------------------------------------------------

def test_dry_run_defaults_to_true():
    """It must take a deliberate act to start writing to real clients."""
    assert Settings().dry_run is True


def test_secrets_are_not_stringified_by_accident():
    s = Settings(avito_client_secret="hunter2", anthropic_api_key="sk-ant-xyz")
    assert "hunter2" not in str(s)
    assert "hunter2" not in repr(s)
    assert "sk-ant-xyz" not in repr(s)


def test_missing_credentials_fail_loudly():
    with pytest.raises(RuntimeError, match="AVITO_CLIENT_ID"):
        Settings(avito_client_id="", avito_client_secret="").require_avito_credentials()


def test_allowed_users_accepts_comma_separated_env_value():
    assert Settings(telegram_allowed_users="111,222").telegram_allowed_users == [111, 222]
    assert Settings(telegram_allowed_users="").telegram_allowed_users == []


def test_allowed_items_accepts_comma_separated_env_value():
    """Именно так значение приходит из Railway. Пробелы после запятой —
    норма для руками собранного списка из 17 идентификаторов."""
    assert Settings(avito_allowed_items="111, 222 ,333").avito_allowed_items == ["111", "222", "333"]


def test_allowed_items_empty_means_everything_is_allowed():
    """Пустая строка и незаданная переменная — «фильтра нет», а не
    «запретить всё»: забытая переменная не должна ронять агента в молчание
    на всех стендах разом."""
    assert Settings(avito_allowed_items="").avito_allowed_items == []
    assert Settings().avito_allowed_items == []


def test_allowed_items_are_strings_even_when_given_as_numbers():
    """item_id в API Авито — число, а у нас везде строка (extract_item_id,
    Chat.item_id). Сравнение строки с числом не совпало бы никогда, причём
    молча — фильтр просто блокировал бы всё подряд."""
    assert Settings(avito_allowed_items=[111, 222]).avito_allowed_items == ["111", "222"]
    assert Settings(avito_allowed_items='["111", "222"]').avito_allowed_items == ["111", "222"]


# --------------------------------------------------------------------------
# Чёрный список: пустое значение = дефолты, а не «ничего не блокировать»
# --------------------------------------------------------------------------

def test_blocklist_empty_env_falls_back_to_defaults():
    """`.env.example` несёт строку `AVITO_BLOCKED_ITEMS=` — скопированная
    целиком, она превращала список в пустой и молча выключала фильтр."""
    from app.config import DEFAULT_BLOCKED_ITEMS

    assert Settings(avito_blocked_items="").avito_blocked_items == list(DEFAULT_BLOCKED_ITEMS)
    assert Settings(avito_blocked_items="   ").avito_blocked_items == list(DEFAULT_BLOCKED_ITEMS)
    assert Settings().avito_blocked_items == list(DEFAULT_BLOCKED_ITEMS)


def test_blocklist_is_disabled_only_by_an_explicit_word():
    assert Settings(avito_blocked_items="none").avito_blocked_items == []
    assert Settings(avito_blocked_items="NONE").avito_blocked_items == []


def test_blocklist_accepts_an_explicit_list():
    assert Settings(avito_blocked_items="111, 222").avito_blocked_items == ["111", "222"]


def test_blocklist_values_are_strings_not_numbers():
    """item_id в API Авито — число; сравнение строки с числом молча не
    совпадёт никогда, то есть запрещённое объявление станет разрешённым."""
    parsed = Settings(avito_blocked_items=[8172444564, 7980739861]).avito_blocked_items
    assert parsed == ["8172444564", "7980739861"]
    assert all(isinstance(item, str) for item in parsed)


def test_default_blocklist_holds_the_known_foreign_listings():
    """7948732527 — второе объявление о продаже всего комплекса. Пять
    остальных ниже — no_keyword_match объявления, денутся по id БЕЗ новых
    ключевых слов (решение заказчика 2026-08-29, чтобы не рисковать ложным
    deny будущих объявлений комплекса). Все найдены живым прогоном
    scripts/sync_item_scope.py против прода (см.
    app/config.py:DEFAULT_BLOCKED_ITEMS)."""
    from app.config import DEFAULT_BLOCKED_ITEMS

    assert set(DEFAULT_BLOCKED_ITEMS) == {
        "8204183112", "8076244626", "8076019723", "7980739861", "8172444564",
        "7948732527",
        "7980333044", "8236197068", "7980615746", "8076853804", "7948469179",
    }


def test_raw_item_id_keeps_the_original_type_for_logs():
    """extract_item_id приводит к строке — из его результата уже не видно,
    что Авито прислал число. Для лога нужен исходный вид."""
    payload = {"payload": {"value": {"item_id": 8172444564}}}
    assert pl.extract_item_id_raw(payload) == 8172444564
    assert pl.extract_item_id(payload) == "8172444564"
    assert pl.extract_item_id_raw({"junk": 1}) is None
