"""Самопроверка разово, из Railway Console:  python -m scripts.selfcheck_now [--send]

Печатает отчёт; с --send ещё и отправляет его оператору в Telegram.
"""

from __future__ import annotations

import asyncio
import sys


async def main() -> None:
    from app.booking.mapping import SqlAlchemyZoneMapping
    from app.booking.yclients import YClientsProvider
    from app.channels.avito import AvitoClient
    from app.channels.avito_items import AvitoItemsClient
    from app.config import get_settings
    from app.db.session import get_sessionmaker
    from app.kb.loader import load_catalog
    from app.kb.override_store import SqlAlchemyOverrideStore, to_overrides
    from app.ops.selfcheck import analyze, collect_facts

    s = get_settings()
    sm = get_sessionmaker()
    kb = load_catalog(overrides=to_overrides(await SqlAlchemyOverrideStore(sm).list_active()))
    zm = SqlAlchemyZoneMapping(sm)
    await zm.load()
    booking = YClientsProvider(
        partner_token=s.yclients_partner_token.get_secret_value(),
        user_token=s.yclients_user_token.get_secret_value(),
        company_id=s.yclients_company_id, mapping=zm, redis=None,
    )
    avito = AvitoClient(settings=s, redis=None)
    items = AvitoItemsClient(settings=s)
    try:
        facts = await collect_facts(
            session_factory=sm, items_client=items, avito_client=avito,
            booking_provider=booking, zone_mapping=zm, settings=s,
        )
        text = analyze(facts, kb).render()
        print(text)
        if "--send" in sys.argv and s.telegram_ops_chat_id:
            from aiogram import Bot

            bot = Bot(token=s.telegram_bot_token.get_secret_value())
            try:
                await bot.send_message(chat_id=s.telegram_ops_chat_id, text=text[:4000])
            finally:
                await bot.session.close()
    finally:
        await avito.aclose()
        await items.aclose()


if __name__ == "__main__":
    asyncio.run(main())
