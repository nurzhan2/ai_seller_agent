"""Чтение списка объявлений продавца — GET /core/v1/items.

Отдельно от avito.py (Messenger), потому что это другая часть API с другим
объектом (объявление, а не чат) — не стоит раздувать один клиент двумя
несвязанными доменами. Транспорт переиспользуется: тот же токен, тот же
BASE_URL, тот же паттерн ретраев.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.channels import avito_endpoints as ep
from app.channels.avito import AvitoAuth, _is_retryable
from app.config import Settings, get_settings
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential


@dataclass(frozen=True)
class Listing:
    item_id: str
    title: str
    url: Optional[str]
    status: str
    price: Optional[int]
    address: Optional[str]


class AvitoItemsClient:
    def __init__(
        self,
        settings: Optional[Settings] = None,
        auth: Optional[AvitoAuth] = None,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.settings = settings or get_settings()
        self.auth = auth or AvitoAuth(self.settings)
        self._client = client or httpx.AsyncClient(
            base_url=ep.BASE_URL, timeout=self.settings.avito_timeout_seconds
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_all_items(self, status: str = "active") -> list[Listing]:
        """Все страницы разом — количество объявлений комплекса невелико
        (порядка десятка), постраничная выдача наружу не нужна."""
        method, path = ep.LIST_ITEMS
        page = 1
        per_page = ep.LIST_ITEMS_MAX_PER_PAGE
        listings: list[Listing] = []

        @retry(
            stop=stop_after_attempt(self.settings.avito_max_retries),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        )
        async def _fetch_page(page_num: int) -> dict:
            token = await self.auth.get_token()
            response = await self._client.request(
                method,
                path,
                headers={ep.AUTH_HEADER: f"{ep.AUTH_SCHEME} {token}"},
                params={"page": page_num, "per_page": per_page, "status": status},
            )
            if response.status_code == 401:
                await self.auth.invalidate()
                token = await self.auth.get_token(force_refresh=True)
                response = await self._client.request(
                    method, path,
                    headers={ep.AUTH_HEADER: f"{ep.AUTH_SCHEME} {token}"},
                    params={"page": page_num, "per_page": per_page, "status": status},
                )
            response.raise_for_status()
            return response.json()

        while True:
            payload = await _fetch_page(page)
            resources = payload.get("resources", [])
            for item in resources:
                listings.append(
                    Listing(
                        item_id=str(item.get("id")),
                        title=str(item.get("title", "")),
                        url=item.get("url"),
                        status=str(item.get("status", "")),
                        price=item.get("price"),
                        address=item.get("address"),
                    )
                )
            if len(resources) < per_page:
                break
            page += 1

        return listings
