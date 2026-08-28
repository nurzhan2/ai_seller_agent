"""Подписка на колбэки Авито — подписаться, посмотреть, отписаться.

Вынесено из `scripts/register_webhook.py`, потому что вызывающих стало двое:
скрипт (руками, при развёртывании) и суточная перепривязка в планировщике
(app/main.py). Переподписка однажды уже чинила молчание вебхука — ненадолго,
но чинила, — и раз она делается по расписанию, у неё и у скрипта обязан быть
ОДИН код. Две копии разъедутся ровно в тот день, когда одну из них поправят.

Секрет в URL не логируется и не печатается: полный адрес собирается здесь,
а наружу отдаётся замаскированный вид (`webhook_url_for_display`).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.channels import avito_endpoints as ep
from app.channels.avito import AvitoAuth
from app.config import Settings, get_settings
from app.webhooks import webhook_path

logger = logging.getLogger("parmangal.avito.webhook")


def webhook_url(base_url: str, settings: Optional[Settings] = None) -> str:
    """Полный адрес вебхука. Содержит секрет — в лог не отдавать."""
    settings = settings or get_settings()
    return base_url.rstrip("/") + webhook_path(settings.require_webhook_secret())


def webhook_url_for_display(base_url: str) -> str:
    return base_url.rstrip("/") + "/webhook/avito/***"


async def _call(
    spec: tuple[str, str],
    payload: dict,
    settings: Optional[Settings] = None,
    auth: Optional[AvitoAuth] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> dict:
    settings = settings or get_settings()
    token = await (auth or AvitoAuth(settings)).get_token()
    method, path = spec

    owned = client is None
    client = client or httpx.AsyncClient(
        base_url=ep.BASE_URL, timeout=settings.avito_timeout_seconds
    )
    try:
        response = await client.request(
            method,
            path,
            headers={ep.AUTH_HEADER: f"{ep.AUTH_SCHEME} {token}"},
            json=payload,
        )
        response.raise_for_status()
        return response.json()
    finally:
        if owned:
            await client.aclose()


async def subscribe(
    base_url: str,
    settings: Optional[Settings] = None,
    auth: Optional[AvitoAuth] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> dict:
    settings = settings or get_settings()
    return await _call(
        ep.WEBHOOK_SUBSCRIBE, {"url": webhook_url(base_url, settings)},
        settings=settings, auth=auth, client=client,
    )


async def unsubscribe(
    base_url: str,
    settings: Optional[Settings] = None,
    auth: Optional[AvitoAuth] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> dict:
    settings = settings or get_settings()
    return await _call(
        ep.WEBHOOK_UNSUBSCRIBE, {"url": webhook_url(base_url, settings)},
        settings=settings, auth=auth, client=client,
    )


async def list_subscriptions(
    settings: Optional[Settings] = None,
    auth: Optional[AvitoAuth] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> dict:
    # Именно POST — так в спеке, см. avito_endpoints.LIST_SUBSCRIPTIONS.
    return await _call(
        ep.LIST_SUBSCRIPTIONS, {}, settings=settings, auth=auth, client=client
    )
