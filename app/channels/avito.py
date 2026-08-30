"""Адаптер Avito Messenger API.

Пути и лимиты берутся исключительно из `avito_endpoints.py` (проверенный
OpenAPI-спек). Здесь — транспорт: кэш и обновление токена, политика ретраев,
ограничение параллелизма, поведение DRY_RUN, гигиена секретов.

Три места, где спек расходится с «интуитивным» REST и где легко ошибиться,
поэтому они помечены и покрыты тестами:
  * POST /token принимает параметры в QUERY STRING, не в теле и не в форме;
  * у GET_MESSAGES завершающий слеш — без него 404;
  * поле загрузки файла называется ровно "uploadfile[]", со скобками.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.channels import avito_endpoints as ep
from app.config import Settings, get_settings
from app.logging_setup import redact_http_logs

logger = logging.getLogger("parmangal.avito")

TOKEN_REDIS_KEY = "avito:access_token"


class SpecNotVerifiedError(RuntimeError):
    """Спек помечен как непроверенный — исходящие запросы запрещены."""


class AvitoTokenError(RuntimeError):
    """Авито отказал в выдаче токена и объяснил, почему.

    Отдельный тип, а не голый KeyError: причина отказа приходит в теле
    ответа (`error`, `error_description`) под кодом HTTP 200, и она нужна
    целиком — по ней различаются «ключи не те», «приложению не разрешён
    этот grant» и «интеграция не активирована в кабинете».
    """


def _assert_spec_verified() -> None:
    if not ep.SPEC_VERIFIED:
        raise SpecNotVerifiedError(
            "app/channels/avito_endpoints.py: SPEC_VERIFIED=False — "
            "запросы к Авито заблокированы."
        )


def _is_retryable(exc: BaseException) -> bool:
    """Ретраим только транспортные сбои и 5xx.

    4xx — это наша ошибка (неверный путь, параметры, отозванный scope), и
    ретрай лишь утраивает вред. 401 обрабатывается отдельно — обновлением
    токена, а не ретраем.
    """
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


def truncate_for_avito(text: str, limit: int = ep.MESSAGE_TEXT_MAX_LENGTH) -> str:
    """Обрезает текст до жёсткого лимита Авито.

    Обрезка живёт здесь, на границе, а не только в агенте: агент целится в
    700 символов, но полагаться на то, что модель всегда уложится, нельзя —
    превышение лимита means отказ доставки, то есть молчание в ответ клиенту.
    Режем по границе слова, чтобы сообщение не обрывалось на полуслове.
    """
    if len(text) <= limit:
        return text

    head = text[: limit - 1]
    cut = head.rstrip()
    space = cut.rfind(" ")
    # Отступаем к границе слова, только если так теряется немного текста.
    if space > limit - 80:
        cut = cut[:space].rstrip()
    return cut + "…"


@dataclass
class _CachedToken:
    value: str
    expires_at: float

    def is_valid(self) -> bool:
        return bool(self.value) and time.monotonic() < self.expires_at


class AvitoAuth:
    """OAuth client_credentials, токен кэшируется в Redis.

    Именно в Redis, а не в памяти процесса: несколько воркеров используют одно
    приложение Авито, иначе каждый выпускал бы свой токен. Локальная копия
    сохраняется, чтобы не ходить в Redis на каждый запрос.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        redis: Any = None,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.settings = settings or get_settings()
        self.redis = redis
        self._client = client
        self._local: Optional[_CachedToken] = None
        self._lock = asyncio.Lock()

    async def get_token(self, *, force_refresh: bool = False) -> str:
        if not force_refresh:
            if self._local and self._local.is_valid():
                return self._local.value
            cached = await self._read_redis()
            if cached:
                return cached

        async with self._lock:
            if not force_refresh and self._local and self._local.is_valid():
                return self._local.value
            return await self._fetch_token()

    async def _read_redis(self) -> Optional[str]:
        if self.redis is None:
            return None
        raw = await self.redis.get(TOKEN_REDIS_KEY)
        if not raw:
            return None
        value = raw.decode() if isinstance(raw, bytes) else str(raw)
        ttl = await self.redis.ttl(TOKEN_REDIS_KEY)
        if isinstance(ttl, int) and ttl > 0:
            self._local = _CachedToken(value, time.monotonic() + ttl)
        return value

    async def _fetch_token(self) -> str:
        _assert_spec_verified()
        client_id, client_secret = self.settings.require_avito_credentials()
        method, path = ep.TOKEN

        client = self._client or httpx.AsyncClient(
            base_url=ep.BASE_URL, timeout=self.settings.avito_timeout_seconds
        )
        try:
            # ВАЖНО: параметры идут в query string (params=), не в теле.
            # Из-за этого client_secret оказывается в URL, а httpx логирует URL
            # целиком на уровне INFO — поэтому запрос идёт под фильтром,
            # вычищающим секреты из логов. См. app/logging_setup.py.
            with redact_http_logs():
                response = await client.request(
                    method,
                    path,
                    params={
                        "grant_type": ep.TOKEN_GRANT_TYPE,
                        "client_id": client_id,
                        "client_secret": client_secret,
                    },
                )
            response.raise_for_status()
            payload = response.json()
        finally:
            if self._client is None:
                await client.aclose()

        # Авито отдаёт ОШИБКУ АВТОРИЗАЦИИ С КОДОМ 200, а не 401: тело вида
        # {"error": "...", "error_description": "..."} приходит под HTTP 200,
        # и raise_for_status выше её не видит. Пока этой ветки не было,
        # провал вылезал как `KeyError: 'access_token'` — трассировка без
        # единого слова о причине, хотя Авито причину назвал прямо в теле.
        # Разбор 2026-08-30 на этом и застрял: в логе был KeyError, а
        # настоящий ответ («The client is not authorized to request a token
        # using this method») пришлось доставать отдельным скриптом
        # (scripts/diagnose_avito_token.py).
        if not isinstance(payload, dict) or "access_token" not in payload:
            error = ""
            description = ""
            if isinstance(payload, dict):
                error = str(payload.get("error") or "")
                description = str(payload.get("error_description") or "")
            logger.error(
                "avito token request rejected: error=%s description=%s (HTTP %s)",
                error or "(нет поля error)",
                description or "(нет поля error_description)",
                response.status_code,
            )
            raise AvitoTokenError(
                f"Авито не выдал токен: {error or 'ответ без access_token'}"
                + (f" — {description}" if description else "")
            )

        token = payload["access_token"]
        expires_in = int(payload.get("expires_in", 86400))
        # Обновляемся на минуту раньше, чтобы запрос в полёте не попал на
        # границу истечения.
        ttl = max(expires_in - self.settings.token_expiry_safety_margin_seconds, 1)

        self._local = _CachedToken(token, time.monotonic() + ttl)
        if self.redis is not None:
            await self.redis.set(TOKEN_REDIS_KEY, token, ex=ttl)

        # Ни токен, ни secret в лог не попадают — только факт и срок жизни.
        logger.info("avito token refreshed", extra={"ttl_seconds": ttl})
        return token

    async def invalidate(self) -> None:
        self._local = None
        if self.redis is not None:
            await self.redis.delete(TOKEN_REDIS_KEY)


class AvitoClient:
    """Асинхронный клиент Messenger API.

    Параллелизм ограничен семафором, а не token bucket: опубликованные лимиты
    привязаны к приложению, а точной квоты у нас нет, поэтому честный рычаг —
    ограничить число одновременных запросов.
    """

    def __init__(
        self,
        settings: Optional[Settings] = None,
        auth: Optional[AvitoAuth] = None,
        client: Optional[httpx.AsyncClient] = None,
        redis: Any = None,
    ):
        self.settings = settings or get_settings()
        self.auth = auth or AvitoAuth(self.settings, redis=redis)
        self._client = client or httpx.AsyncClient(
            base_url=ep.BASE_URL, timeout=self.settings.avito_timeout_seconds
        )
        self._semaphore = asyncio.Semaphore(self.settings.avito_max_concurrency)

    @property
    def user_id(self) -> str:
        return self.settings.avito_user_id

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- ядро --------------------------------------------------------------

    def _log_rate_limit(self, response: httpx.Response) -> None:
        limit_header, remaining_header = ep.RATE_LIMIT_HEADERS
        remaining = response.headers.get(remaining_header)
        if remaining is None:
            return
        try:
            left = int(remaining)
        except ValueError:
            return
        if left <= 5:
            logger.warning(
                "avito rate limit nearly exhausted",
                extra={"remaining": left, "limit": response.headers.get(limit_header)},
            )

    async def _request(self, spec: tuple[str, str], path: str, **kwargs: Any) -> httpx.Response:
        _assert_spec_verified()
        method = spec[0]

        @retry(
            stop=stop_after_attempt(self.settings.avito_max_retries),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        )
        async def _attempt() -> httpx.Response:
            async with self._semaphore:
                token = await self.auth.get_token()
                headers = {
                    **kwargs.pop("headers", {}),
                    ep.AUTH_HEADER: f"{ep.AUTH_SCHEME} {token}",
                }
                response = await self._client.request(method, path, headers=headers, **kwargs)

                if response.status_code == 401:
                    # Токен умер раньше срока (отозван, ротирован). Обновляем
                    # и повторяем внутри той же попытки — это не транспортный
                    # сбой, и он не должен съедать попытку tenacity.
                    await self.auth.invalidate()
                    fresh = await self.auth.get_token(force_refresh=True)
                    headers[ep.AUTH_HEADER] = f"{ep.AUTH_SCHEME} {fresh}"
                    response = await self._client.request(
                        method, path, headers=headers, **kwargs
                    )

                self._log_rate_limit(response)
                response.raise_for_status()
                return response

        return await _attempt()

    # -- чаты --------------------------------------------------------------

    async def list_chats(self, *, limit: int = 50, offset: int = 0) -> dict:
        spec = ep.LIST_CHATS
        path = spec[1].format(user_id=self.user_id)
        response = await self._request(spec, path, params={"limit": limit, "offset": offset})
        return response.json()

    async def get_chat(self, chat_id: str) -> dict:
        spec = ep.GET_CHAT
        path = spec[1].format(user_id=self.user_id, chat_id=chat_id)
        return (await self._request(spec, path)).json()

    async def get_messages(self, chat_id: str, *, limit: int = 50, offset: int = 0) -> dict:
        # У этого пути завершающий слеш — см. avito_endpoints.GET_MESSAGES.
        spec = ep.GET_MESSAGES
        path = spec[1].format(user_id=self.user_id, chat_id=chat_id)
        response = await self._request(spec, path, params={"limit": limit, "offset": offset})
        return response.json()

    async def mark_chat_read(self, chat_id: str) -> dict:
        spec = ep.MARK_CHAT_READ
        path = spec[1].format(user_id=self.user_id, chat_id=chat_id)
        return (await self._request(spec, path)).json()

    # -- отправка ----------------------------------------------------------

    async def send_message(self, chat_id: str, text: str) -> dict:
        """Отправка текста живому клиенту.

        В DRY_RUN не доходит до Авито: вызывающий код обязан сохранить
        подготовленное сообщение со статусом `dry_run` и продублировать его
        оператору. Проверка продублирована здесь, чтобы пропущенная проверка
        выше по стеку не привела к сообщению реальному клиенту.
        """
        safe_text = truncate_for_avito(text)
        if safe_text != text:
            logger.warning(
                "avito message truncated",
                extra={
                    "chat_id": chat_id,
                    "original_length": len(text),
                    "sent_length": len(safe_text),
                    "limit": ep.MESSAGE_TEXT_MAX_LENGTH,
                },
            )

        if self.settings.dry_run:
            logger.info(
                "DRY_RUN: message withheld",
                extra={"chat_id": chat_id, "length": len(safe_text)},
            )
            return {"dry_run": True, "chat_id": chat_id, "text": safe_text}

        spec = ep.SEND_MESSAGE
        path = spec[1].format(user_id=self.user_id, chat_id=chat_id)
        response = await self._request(
            spec, path, json={"message": {"text": safe_text}, "type": "text"}
        )
        return response.json()

    async def upload_image(self, image_bytes: bytes, filename: str = "photo.jpg") -> dict:
        """Шаг 1 из 2. Имя поля — ровно "uploadfile[]", вместе со скобками."""
        spec = ep.UPLOAD_IMAGES
        path = spec[1].format(user_id=self.user_id)
        response = await self._request(
            spec,
            path,
            files={ep.UPLOAD_IMAGES_FIELD: (filename, image_bytes, "image/jpeg")},
        )
        return response.json()

    async def send_image(self, chat_id: str, image_id: str) -> dict:
        """Шаг 2 из 2 — отправка уже загруженного изображения."""
        if self.settings.dry_run:
            logger.info(
                "DRY_RUN: image withheld",
                extra={"chat_id": chat_id, "image_id": image_id},
            )
            return {"dry_run": True, "chat_id": chat_id, "image_id": image_id}

        spec = ep.SEND_IMAGE
        path = spec[1].format(user_id=self.user_id, chat_id=chat_id)
        return (await self._request(spec, path, json={"image_id": image_id})).json()

    async def upload_and_send_image(
        self, chat_id: str, image_bytes: bytes, filename: str = "photo.jpg"
    ) -> dict:
        """Оба шага сразу.

        В разобранных диалогах фото просили четыре раза и ни разу не прислали,
        после чего два диалога оборвались (docs/analysis/failures.md), поэтому
        отправка фото должна быть одним вызовом, а не ритуалом из двух.
        """
        uploaded = await self.upload_image(image_bytes, filename)
        image_id = _extract_image_id(uploaded)
        if image_id is None:
            raise ValueError(f"uploadImages вернул неожиданный ответ: {uploaded!r}")
        return await self.send_image(chat_id, image_id)


def _extract_image_id(uploaded: dict) -> Optional[str]:
    """Ответ uploadImages — словарь вида {"<image_id>": {...размеры...}}."""
    if not isinstance(uploaded, dict) or not uploaded:
        return None
    for key in ("image_id", "id"):
        value = uploaded.get(key)
        if isinstance(value, str) and value:
            return value
    first_key = next(iter(uploaded))
    return str(first_key) if first_key else None
