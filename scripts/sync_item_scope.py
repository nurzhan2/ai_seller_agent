"""Разовый прогон часовой классификации объявлений — вручную, не дожидаясь
следующего прохода фонового воркера (app/main.py:supervised_item_scope_refresh).

    python -m scripts.sync_item_scope

Тянет все объявления аккаунта (AvitoItemsClient.list_all_items, статусы —
settings.poller_items_statuses, по умолчанию active,old,removed),
классифицирует каждое по заголовку (app/channels/item_scope.py:
classify_listing — жёсткий deny по AVITO_BLOCKED_ITEMS поверх классификации
по словам) и пишет результат в таблицу item_scope. Печатает то же самое
таблицей на экран — ответ на вопрос «какие объявления аккаунта получили
allow, а какие deny» без похода в БД руками.

НИЧЕГО НЕ ОТПРАВЛЯЕТ клиентам — только читает список объявлений и пишет
классификацию в свою же таблицу item_scope.
"""

from __future__ import annotations

import asyncio
import sys

from app.channels.avito_items import AvitoItemsClient
from app.channels.item_scope import (
    SqlAlchemyItemScopeStore,
    classify_listing,
    hard_deny_ids_from_settings,
)
from app.config import get_settings
from app.db.session import get_sessionmaker


async def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    settings = get_settings()
    if not settings.avito_user_id or "your_" in settings.avito_user_id:
        print(
            "AVITO_USER_ID не задан (в .env шаблонное значение) — запросы к "
            "Авито невозможны. Прогон возможен только там, где выставлены "
            "настоящие AVITO_CLIENT_ID/AVITO_CLIENT_SECRET/AVITO_USER_ID.",
            file=sys.stderr,
        )
        return 1

    items_client = AvitoItemsClient(settings=settings)
    store = SqlAlchemyItemScopeStore(get_sessionmaker())
    hard_deny_ids = hard_deny_ids_from_settings(settings)

    try:
        listings = await items_client.list_all_items(status=settings.poller_items_statuses)
    finally:
        await items_client.aclose()

    rows = []
    for listing in listings:
        decision, reason = classify_listing(listing.item_id, listing.title, hard_deny_ids)
        await store.upsert(listing.item_id, title=listing.title, decision=decision, reason=reason)
        rows.append((listing.item_id, listing.title, decision, reason))

    rows.sort(key=lambda r: (r[2], r[1] or ""))

    print(f"Объявлений классифицировано: {len(rows)} (статусы: {settings.poller_items_statuses})")
    print()
    header = f"{'item_id':<14} {'решение':<7} {'причина':<24} заголовок"
    print(header)
    print("-" * len(header))
    for item_id, title, decision, reason in rows:
        print(f"{item_id:<14} {decision:<7} {reason:<24} {title}")

    allowed = sum(1 for r in rows if r[2] == "allow")
    denied = len(rows) - allowed
    print()
    print(f"ИТОГО: allow {allowed}, deny {denied}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
