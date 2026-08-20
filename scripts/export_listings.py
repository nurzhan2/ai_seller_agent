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

from app.channels.avito_items import AvitoItemsClient

DEFAULT_OUT = Path("docs/avito_listings.csv")


async def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"куда писать CSV (по умолчанию {DEFAULT_OUT})")
    parser.add_argument(
        "--status", default="active",
        help="статусы через запятую: active,removed,old,blocked,rejected (по умолчанию active)",
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
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
