"""Выгрузка объявлений Авито в CSV: item_id, заголовок, ссылка.

    python -m scripts.export_listings
    python -m scripts.export_listings --out docs/avito_listings.csv
    python -m scripts.export_listings --status active,old

Заказчику для этого ничего делать не нужно — данные берутся через API по
уже настроенным ключам приложения (AVITO_CLIENT_ID/SECRET в .env).

Итоговый файл — рабочий материал для маппинга item_id → zone_id
(app/booking/mapping.py, ZoneServiceMap): заказчик уже сказал, что отдельных
объявлений на каждую баню нет, они общие, так что построчного соответствия
"одно объявление — одна зона" не будет, и это нормально — см. README о
неоднозначных объявлениях.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from pathlib import Path

from app.channels.avito_items import AvitoItemsClient, Listing

DEFAULT_OUT = Path("docs/avito_listings.csv")


async def seed_item_zone_map(listings: list[Listing]) -> int:
    """Записать item_id + заголовок в item_zone_map, вернуть число строк.

    ТОЛЬКО заголовок: zone_id и category — решение человека о том, какая
    зона стоит за объявлением (у гриль-домика их шесть, у бани «Гараж» —
    две), и перетирать это выгрузкой из Авито нельзя. У существующих строк
    обновляется одно поле title, остальные не трогаются.
    """
    from sqlalchemy import select

    from app.db.models import ItemZoneMap
    from app.db.session import get_sessionmaker

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        existing = {
            row.item_id: row
            for row in (await session.execute(select(ItemZoneMap))).scalars().all()
        }
        for listing in listings:
            row = existing.get(listing.item_id)
            if row is None:
                session.add(ItemZoneMap(item_id=listing.item_id, title=listing.title))
            else:
                row.title = listing.title
        await session.commit()
    return len(listings)


async def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"куда писать CSV (по умолчанию {DEFAULT_OUT})")
    parser.add_argument(
        "--status", default="active",
        help="статусы через запятую: active,removed,old,blocked,rejected (по умолчанию active)",
    )
    parser.add_argument(
        "--seed-map", action="store_true",
        help="дополнительно записать item_id и заголовок в item_zone_map "
             "(zone_id/category не трогаются — это ручной маппинг)",
    )
    args = parser.parse_args()

    client = AvitoItemsClient()
    try:
        listings = await client.list_all_items(status=args.status)
    finally:
        await client.aclose()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["item_id", "title", "url", "status", "price", "address"])
        for listing in listings:
            writer.writerow(
                [listing.item_id, listing.title, listing.url or "", listing.status,
                 listing.price if listing.price is not None else "", listing.address or ""]
            )

    print(f"Объявлений: {len(listings)}")
    print(f"Файл: {args.out}")

    if args.seed_map:
        seeded = await seed_item_zone_map(listings)
        print(f"В item_zone_map записано заголовков: {seeded} "
              "(zone_id/category не изменялись)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
