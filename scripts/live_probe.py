"""Живой прогон агента на проде БЕЗ отправки в Авито и без записи броней.

Запуск в контейнере Railway:  python -m scripts.live_probe
Берёт настоящие KB (с overrides), YCLIENTS (только чтение занятости),
LLM и сопоставление объявлений из базы. Печатает ответы бота и фото.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta

from app.agent.loop import AgentLoop
from app.agent.providers.factory import build_provider, resolve_models
from app.booking.mapping import SqlAlchemyZoneMapping
from app.booking.yclients import YClientsProvider
from app.config import get_settings
from app.db.session import get_sessionmaker
from app.dialog_store import SqlAlchemyDialogStore
from app.kb.loader import load_catalog
from app.kb.override_store import SqlAlchemyOverrideStore, to_overrides
from app.media.photos import KbPhotoProvider


def _next(weekday: int) -> date:
    d = date.today() + timedelta(days=1)
    while d.weekday() != weekday:
        d += timedelta(days=1)
    return d


SAT = _next(5).strftime("%d.%m")
WED = _next(2).strftime("%d.%m")

SCENARIOS = [
    ("беседки 1000 (купол)", "7948042872", [
        f"Здравствуйте, сколько стоит аренда беседки {SAT} с 17 до 21?",
        "А фото можно?",
    ]),
    ("беседка весь день 4999 (купол)", "8044565629", [
        f"Можно забронировать беседку на весь день {WED}?",
        "Сколько стоит?",
    ]),
    ("гриль-домик", "7852138383", [
        f"Добрый вечер, гриль-домик на {SAT} с 18 до 21 свободен? сколько?",
    ]),
    ("гриль весь день будни", "8140245946", [
        f"Гриль-домик на весь день {WED}, цена?",
    ]),
    ("русская баня", "8314309619", [
        f"Баня на {SAT} на 3 часа вечером, сколько и есть ли время?",
        "Пришлите фото",
    ]),
    ("баня рыцаря", "8326308024", [
        f"Замок рыцаря {WED} с 15:00 на 2 часа, свободно?",
    ]),
    ("шатёр", "7980683885", [
        f"Шатёр на 25 человек {SAT} с 14 до 20, сколько?",
    ]),
    ("домик весь день", "8043927867", [
        f"Здравствуйте, домик на весь день {WED} свободен? Сколько стоит?",
    ]),
    ("сертификат", "8512379829", [
        "Здравствуйте, хочу сертификат в баню на юбилей мужу",
        "На 10 тысяч",
    ]),
    ("купол цена сразу", "7916557086", [
        f"Сколько стоит беседка {SAT} с 15 до 19?",
    ]),
]


async def main() -> None:
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
    store = SqlAlchemyDialogStore(sm)
    dialog_model, classifier_model = resolve_models(s)
    loop = AgentLoop(
        client=build_provider(s), kb=kb, dialog_model=dialog_model,
        classifier_model=classifier_model, booking_provider=booking,
        photo_provider=KbPhotoProvider(lambda: kb),
    )
    only = sys.argv[1:] or None
    for name, item_id, turns in SCENARIOS:
        if only and not any(o in name or o == item_id for o in only):
            continue
        print(f"\n=== {name} (item {item_id})", flush=True)
        history: list[dict] = []
        for text in turns:
            try:
                r = await loop.run_turn(
                    f"probe-{item_id}", history, text,
                    item_id=item_id, item_lookup=store,
                )
            except Exception as e:  # noqa: BLE001
                print("  !! ERROR", type(e).__name__, e, flush=True)
                break
            print(f"  К: {text}")
            print(f"  Б: {(r.text or '').strip()}")
            photos = getattr(r, "photos", None) or []
            if photos or r.escalated:
                print(f"     фото={len(photos)} эскалация={r.escalated} {r.escalation_reason or ''}")
            history += [{"role": "user", "content": text},
                        {"role": "assistant", "content": r.text or ""}]


if __name__ == "__main__":
    asyncio.run(main())
