"""Связать наши зоны с услугами YCLIENTS — сопоставление названий, вручную.

    python -m scripts.sync_yclients_services              # только показать
    python -m scripts.sync_yclients_services --apply       # показать, спросить, записать подтверждённое

ПОЧЕМУ ЭТОТ СКРИПТ СУЩЕСТВУЕТ. Раньше пустой `zone_service_map` читался в
`/admin/booking` как «каталог услуг у заказчика пуст». Это была ошибка
диагностики: заказчик подтвердил, что услуги в YCLIENTS заведены. Пустая
таблица здесь означает только то, что МЫ их не связали с нашими zone_id —
общего идентификатора между двумя каталогами нет, связать их может только
человек, сверив названия глазами.

ПОЧЕМУ СОПОСТАВЛЕНИЕ НЕ ПРИМЕНЯЕТСЯ МОЛЧА. Названия в YCLIENTS почти
наверняка сформулированы иначе, чем в нашем каталоге («Баня «Русский
стиль»» у нас против, например, «русская баня» у заказчика) — точное
совпадение бесполезно, а порог схожести всегда может ошибиться. Неверная
связка — не косметическая проблема: `check_availability` начнёт проверять
занятость ЧУЖОЙ зоны, и агент скажет клиенту, что время свободно, когда на
самом деле занята другая баня, а выбранная клиентом просто ни разу не
проверялась. Поэтому каждая пара подтверждается человеком по отдельности,
и без --apply скрипт вообще ничего не пишет.

staff_id/company_id — тоже НЕ автоматически. У YCLIENTS нет эндпоинта
списка сотрудников/ресурсов в утверждённой спецификации
(app/booking/yclients_endpoints.py) — их можно только ввести вручную,
посмотрев в личном кабинете YCLIENTS. Без staff_id `get_free_slots` уйдёт
на служебный `staff_id="0"` (см. app/booking/yclients.py) — скорее всего
не тот ресурс, поэтому шаг 4 (проверка check_availability сразу после
записи) существует не для галочки: если он вернул не FREE/BUSY, а
UNKNOWN — staff_id почти наверняка не тот или пуст, чинить нужно СЕЙЧАС,
а не после жалобы живого клиента.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import date as DateType, timedelta
from typing import Optional

from app.booking.base import Service
from app.booking.mapping import SqlAlchemyZoneMapping
from app.booking.yclients import YClientsProvider
from app.config import get_settings
from app.db.session import get_sessionmaker
from app.kb.loader import KnowledgeBase, load_catalog
from app.logging_setup import configure_logging
from app.media.photo_import import normalize_name

# Та же грубая «основа слова», что и в app/agent/tools.py::_stems, для
# сравнения тем FAQ — 4 первых символа покрывают русские падежи и склонения
# без словаря и без новой зависимости в requirements.txt. Не импортируется
# оттуда: имя там подчёркнутое (внутренняя деталь агента), а не публичный
# интерфейс — считаем совпадение подхода осознанным, а не общим кодом.
_STEM_LENGTH = 4

# Ниже этого порога совпадение не предлагается вовсе — лучше явное «нет
# кандидата», чем уверенно выглядящая, но случайная пара.
MIN_SCORE = 0.3


def _stems(text: str) -> set[str]:
    return {w[:_STEM_LENGTH] for w in normalize_name(text).split() if len(w) >= _STEM_LENGTH}


def _similarity(a: str, b: str) -> float:
    """Jaccard по основам слов: 0 — ничего общего, 1 — то же самое."""
    sa, sb = _stems(a), _stems(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


@dataclass(frozen=True)
class Candidate:
    service: Service
    score: float


def best_candidates(zone_names: list[str], services: list[Service], limit: int = 3) -> list[Candidate]:
    """Лучшие `limit` услуг для зоны, по максимуму схожести среди всех имён
    зоны (name и display_name_alt — заказчик иногда называет зону иначе,
    чем она записана у нас, см. app/kb/loader.py:Zone)."""
    scored = [
        Candidate(service=s, score=max(_similarity(name, s.title) for name in zone_names))
        for s in services
    ]
    scored = [c for c in scored if c.score >= MIN_SCORE]
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored[:limit]


def _money(candidate: Service) -> str:
    if candidate.price_min is None and candidate.price_max is None:
        return "цена неизвестна"
    if candidate.price_min == candidate.price_max or candidate.price_max is None:
        return f"{candidate.price_min} ₽"
    return f"{candidate.price_min}–{candidate.price_max} ₽"


def _duration(candidate: Service) -> str:
    if candidate.duration_seconds is None:
        return "длительность неизвестна"
    minutes = candidate.duration_seconds // 60
    return f"{minutes} мин" if minutes < 60 else f"{minutes / 60:g} ч"


def print_services_table(services: list[Service]) -> None:
    print(f"\nУслуги в YCLIENTS ({len(services)}):")
    if not services:
        print("  (пусто — см. примечание в /admin/booking: это состояние YCLIENTS, не наша связка)")
        return
    print(f"  {'service_id':<12} {'название':<40} {'длительность':<16} цена")
    for s in services:
        print(f"  {s.service_id:<12} {s.title[:40]:<40} {_duration(s):<16} {_money(s)}")


def _zone_names(zone) -> list[str]:
    names = [zone.name]
    if zone.display_name_alt:
        names.append(zone.display_name_alt)
    return names


def _prompt(question: str) -> Optional[str]:
    try:
        return input(question).strip()
    except EOFError:
        # Нет интерактивного stdin (например, случайно запустили не в
        # консоли). Возвращаем None, а не "" — иначе неотличимо от
        # обычного Enter, и confirm_and_apply молча принял бы предложенное
        # вместо того, чтобы пропустить зону.
        return None


def propose_mapping(
    kb: KnowledgeBase, services: list[Service], existing: dict[str, dict],
) -> dict[str, Candidate]:
    """Кандидат на КАЖДУЮ зону, у которой ещё нет связки. Возвращает только
    зоны, для которых нашёлся хоть один кандидат выше MIN_SCORE."""
    proposals: dict[str, Candidate] = {}
    print("\nПредлагаемое сопоставление (зона -> услуга YCLIENTS):")
    for zone in kb.catalog.zones:
        if zone.id in existing:
            continue
        candidates = best_candidates(_zone_names(zone), services)
        if not candidates:
            print(f"  {zone.id:<20} {zone.name:<35} — нет похожей услуги (заведите вручную)")
            continue
        top = candidates[0]
        proposals[zone.id] = top
        print(f"  {zone.id:<20} {zone.name:<35} -> [{top.service.service_id}] "
              f"{top.service.title}  (схожесть {top.score:.0%})")
        for alt in candidates[1:]:
            print(f"  {'':<20} {'':<35}    вариант: [{alt.service.service_id}] "
                  f"{alt.service.title}  (схожесть {alt.score:.0%})")
    return proposals


async def confirm_and_apply(
    kb: KnowledgeBase, proposals: dict[str, Candidate], mapping: SqlAlchemyZoneMapping,
) -> None:
    print("\nПодтверждение по каждой зоне (Enter — принять предложенное, "
          "или впишите свой service_id, «n» — пропустить зону):")
    for zone in kb.catalog.zones:
        candidate = proposals.get(zone.id)
        if candidate is None:
            continue

        answer = _prompt(
            f"\n{zone.id} ({zone.name}) -> [{candidate.service.service_id}] "
            f"{candidate.service.title}. Принять? [Y/n/service_id]: "
        )
        if answer is None:
            print(f"  пропущено (нет интерактивного ввода): {zone.id}")
            continue
        if answer.lower() == "n":
            print(f"  пропущено: {zone.id}")
            continue
        service_id = answer if answer and answer.lower() != "y" else candidate.service.service_id

        staff_id = _prompt(
            "  staff_id в YCLIENTS для этой зоны (можно посмотреть в личном "
            "кабинете; Enter — оставить пустым, тогда будет использован "
            "служебный «0», который почти наверняка НЕ тот ресурс): "
        )
        company_id = _prompt(
            f"  company_id (Enter — взять из настроек, {get_settings().yclients_company_id or 'не задан'}): "
        )

        await mapping.set(
            zone.id,
            service_id=service_id,
            staff_id=staff_id or None,
            company_id=company_id or get_settings().yclients_company_id or None,
        )
        print(f"  записано: {zone.id} -> service_id={service_id}"
              f"{', staff_id=' + staff_id if staff_id else ' (staff_id пуст!)'}")


async def verify_live(provider: YClientsProvider, kb: KnowledgeBase, mapping: SqlAlchemyZoneMapping,
                       check_date: DateType) -> None:
    """Пункт 4: реальный check_availability на только что записанных
    зонах. UNKNOWN здесь — не абстрактный риск, а сигнал «что-то из
    только что введённого не так», и его нужно увидеть сразу, а не после
    жалобы клиента."""
    mapped_zone_ids = mapping.mapped_zones()
    if not mapped_zone_ids:
        print("\nНечего проверять — ни одна зона не связана.")
        return

    print(f"\nПроверка занятости на {check_date.isoformat()}:")
    for zone_id in mapped_zone_ids:
        availability = await provider.check_availability(zone_id, check_date)
        zone = next((z for z in kb.catalog.zones if z.id == zone_id), None)
        label = f"{zone_id} ({zone.name})" if zone else zone_id
        if availability.status.value == "unknown":
            print(f"  ⚠ {label}: UNKNOWN — {availability.reason or 'причина не указана'}. "
                  f"Связка не работает, проверьте service_id/staff_id.")
        else:
            print(f"  ✓ {label}: {availability.status.value.upper()}"
                  f"{' — свободные слоты: ' + ', '.join(availability.free_slots) if availability.free_slots else ''}")


async def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    configure_logging()

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="спросить подтверждение по каждой зоне и записать в zone_service_map")
    parser.add_argument("--check-date", type=DateType.fromisoformat, default=None,
                        help="дата для проверки check_availability после записи (по умолчанию — завтра)")
    args = parser.parse_args()
    check_date = args.check_date or (DateType.today() + timedelta(days=1))

    settings = get_settings()
    if not settings.yclients_partner_token.get_secret_value():
        print("YCLIENTS_PARTNER_TOKEN не задан — нечего синхронизировать.", file=sys.stderr)
        return 1

    kb = load_catalog()
    zone_mapping = SqlAlchemyZoneMapping(get_sessionmaker())
    await zone_mapping.load()
    provider = YClientsProvider(
        partner_token=settings.yclients_partner_token.get_secret_value(),
        user_token=settings.yclients_user_token.get_secret_value(),
        company_id=settings.yclients_company_id,
        mapping=zone_mapping,
    )

    services = await provider.get_services()
    print_services_table(services)
    if not services:
        print("\nСписок услуг пуст — проверьте, подключена ли интеграция филиалом "
              "в личном кабинете YCLIENTS (см. app/booking/yclients_endpoints.py).")
        return 1

    existing = {zid: zone_mapping.get(zid) for zid in zone_mapping.mapped_zones()}
    if existing:
        print(f"\nУже связаны ({len(existing)}): {', '.join(sorted(existing))}")

    proposals = propose_mapping(kb, services, existing)
    if not proposals:
        print("\nВсе зоны уже связаны, либо для оставшихся не нашлось похожей услуги.")

    if not args.apply:
        print("\n--apply не указан: ничего не записано. "
              "Проверьте предложенные пары и перезапустите с --apply.")
        return 0

    if not proposals:
        return 0

    await confirm_and_apply(kb, proposals, zone_mapping)
    await verify_live(provider, kb, zone_mapping, check_date)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
