"""Связать наши зоны с сотрудниками YCLIENTS — сопоставление названий, вручную.

    python -m scripts.sync_yclients_services              # только показать
    python -m scripts.sync_yclients_services --apply       # показать, спросить, записать подтверждённое

ПОЧЕМУ СТАФФ, А НЕ УСЛУГА. Первая версия этого скрипта сопоставляла зону с
service_id — казалось логичным: зона это то, что бронируют, а book_services
это список того, что можно забронировать. Разведка через
scripts/inspect_yclients.py показала обратное устройство кабинета заказчика:

  * staff (9 объектов) — это и есть ЗОНЫ комплекса (Юрта, Шатёр, Купол со
    стульями и т.д.). Занятость зоны в YCLIENTS определяется занятостью
    СОТРУДНИКА — book_times спрашивает про staff_id, не про service_id
    (см. app/booking/yclients.py:get_free_slots, BOOK_TIMES).
  * book_services (35 объектов) — это варианты брони: зона × длительность ×
    тип дня («Баня Гараж на 3 часа в будние дни»). Для проверки занятости
    не нужны вообще; понадобятся позже, при создании самой брони
    (create_booking шлёт services=[...] — см. app/booking/yclients.py).

Поэтому связка зона -> staff_id — это то, что делает работать
check_availability. service_id в zone_service_map по-прежнему можно
указать (используется потом при бронировании), но матчится и подтверждается
здесь только staff_id: 35 услуг на 9-10 зон не сопоставляются 1:1
автоматически, это трёхмерная задача (зона, длительность, день), а не
задача этого скрипта.

Зон у нас 10, сотрудников у заказчика 9 — какая-то зона гарантированно
останется без пары, скрипт обязан явно её показать, а не смолчать.

ПОЧЕМУ СОПОСТАВЛЕНИЕ НЕ ПРИМЕНЯЕТСЯ МОЛЧА. Имена сотрудников в YCLIENTS
почти наверняка сформулированы иначе, чем в нашем каталоге, а порог
схожести всегда может ошибиться. Неверная связка — не косметическая
проблема: check_availability начнёт проверять занятость ЧУЖОЙ зоны, и агент
скажет клиенту, что время свободно, когда на самом деле занята другая баня.
Поэтому каждая пара подтверждается человеком по отдельности, и без --apply
скрипт вообще ничего не пишет. Отдельная проверка (см.
_find_duplicate_staff_assignments) ловит ещё более опасный случай — один и
тот же сотрудник предложен сразу для двух разных зон.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import unicodedata
from dataclasses import dataclass
from datetime import date as DateType, timedelta
from typing import Optional

from app.booking.base import Service, Staff
from app.booking.mapping import SqlAlchemyZoneMapping
from app.booking.yclients import YClientsProvider
from app.config import get_settings
from app.db.session import get_sessionmaker
from app.kb.loader import KnowledgeBase, load_catalog
from app.logging_setup import configure_logging

# Та же грубая «основа слова», что и в app/agent/tools.py::_stems, для
# сравнения тем FAQ — 4 первых символа покрывают русские падежи и склонения
# без словаря и без новой зависимости в requirements.txt. Не импортируется
# оттуда: имя там подчёркнутое (внутренняя деталь агента), а не публичный
# интерфейс — считаем совпадение подхода осознанным, а не общим кодом.
_STEM_LENGTH = 4

# Ниже этого порога совпадение не предлагается вовсе — лучше явное «нет
# кандидата», чем уверенно выглядящая, но случайная пара.
MIN_SCORE = 0.3


def _normalize_name(name: str) -> str:
    """Регистр, «ё», дефисы/подчёркивания/лишние пробелы — не важны.

    Та же логика, что и app/media/photo_import.py::normalize_name, но
    продублирована, а не импортирована: app/media/ подпадает под шаблон
    "media" в .gitignore/.dockerignore (для реальных фото зон) и поэтому
    не гарантированно доступен ни в git-чекауте, ни в образе — скрипт для
    Railway Console не может зависеть от модуля, который может там не
    оказаться.
    """
    text = unicodedata.normalize("NFKC", name).lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9]+", " ", text)
    return " ".join(text.split())


def _stems(text: str) -> set[str]:
    return {w[:_STEM_LENGTH] for w in _normalize_name(text).split() if len(w) >= _STEM_LENGTH}


def _similarity(a: str, b: str) -> float:
    """Jaccard по основам слов: 0 — ничего общего, 1 — то же самое."""
    sa, sb = _stems(a), _stems(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


@dataclass(frozen=True)
class Candidate:
    staff: Staff
    score: float


def best_candidates(zone_names: list[str], staff_list: list[Staff], limit: int = 3) -> list[Candidate]:
    """Лучшие `limit` сотрудников для зоны, по максимуму схожести среди всех
    имён зоны (name и display_name_alt — заказчик иногда называет зону
    иначе, чем она записана у нас, см. app/kb/loader.py:Zone)."""
    scored = [
        Candidate(staff=s, score=max(_similarity(name, s.name) for name in zone_names))
        for s in staff_list
    ]
    scored = [c for c in scored if c.score >= MIN_SCORE]
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored[:limit]


def _money(service: Service) -> str:
    if service.price_min is None and service.price_max is None:
        return "цена неизвестна"
    if service.price_min == service.price_max or service.price_max is None:
        return f"{service.price_min} ₽"
    return f"{service.price_min}–{service.price_max} ₽"


def _duration(service: Service) -> str:
    if service.duration_seconds is None:
        return "длительность неизвестна"
    minutes = service.duration_seconds // 60
    return f"{minutes} мин" if minutes < 60 else f"{minutes / 60:g} ч"


def print_staff_table(staff_list: list[Staff]) -> None:
    """Пункт 1 задачи: полный список сотрудников — это и есть зоны в
    кабинете заказчика, показываем все, не только совпавшие."""
    print(f"\nСотрудники в YCLIENTS ({len(staff_list)}) — физически это зоны комплекса:")
    if not staff_list:
        print("  (пусто — проверьте scripts/inspect_yclients.py, раздел staff)")
        return
    print(f"  {'staff_id':<12} название")
    for s in staff_list:
        print(f"  {s.staff_id:<12} {s.name}")


def print_services_table(services: list[Service]) -> None:
    """Пункт 3 задачи: полный список услуг — отдельно от сопоставления зон,
    только для того, чтобы понять, как варианты брони (зона×длительность×
    день) соотносятся с зонами. Автоматически ни на что не матчится."""
    print(f"\nУслуги в YCLIENTS ({len(services)}) — варианты брони "
          f"(зона × длительность × тип дня), не сотрудники:")
    if not services:
        print("  (пусто — см. примечание в /admin/booking: это состояние YCLIENTS, не наша связка)")
        return
    print(f"  {'service_id':<12} {'название':<48} {'длительность':<16} цена")
    for s in services:
        print(f"  {s.service_id:<12} {s.title[:48]:<48} {_duration(s):<16} {_money(s)}")


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
    kb: KnowledgeBase, staff_list: list[Staff], existing: dict[str, dict],
) -> dict[str, Candidate]:
    """Кандидат на КАЖДУЮ зону, у которой ещё нет связки. Возвращает только
    зоны, для которых нашёлся хоть один кандидат выше MIN_SCORE."""
    proposals: dict[str, Candidate] = {}
    print("\nПредлагаемое сопоставление (зона -> сотрудник YCLIENTS = зона в кабинете):")
    for zone in kb.catalog.zones:
        if zone.id in existing:
            continue
        candidates = best_candidates(_zone_names(zone), staff_list)
        if not candidates:
            print(f"  {zone.id:<20} {zone.name:<35} — нет похожего сотрудника (заведите вручную)")
            continue
        top = candidates[0]
        proposals[zone.id] = top
        print(f"  {zone.id:<20} {zone.name:<35} -> [{top.staff.staff_id}] "
              f"{top.staff.name}  (схожесть {top.score:.0%})")
        for alt in candidates[1:]:
            print(f"  {'':<20} {'':<35}    вариант: [{alt.staff.staff_id}] "
                  f"{alt.staff.name}  (схожесть {alt.score:.0%})")
    return proposals


def _find_duplicate_staff_assignments(proposals: dict[str, Candidate]) -> dict[str, list[str]]:
    """Один и тот же сотрудник предложен сразу для двух зон — опаснее, чем
    зона без пары: значит check_availability будет проверять занятость
    ОДНОГО физического места для двух разных зон каталога."""
    by_staff: dict[str, list[str]] = {}
    for zone_id, candidate in proposals.items():
        by_staff.setdefault(candidate.staff.staff_id, []).append(zone_id)
    return {sid: zones for sid, zones in by_staff.items() if len(zones) > 1}


def print_unmatched_and_conflicts(
    kb: KnowledgeBase, proposals: dict[str, Candidate], existing: dict[str, dict],
) -> None:
    """Пункт 1 задачи: явно показать зону без пары. У заказчика 9
    сотрудников на 10 зон — минимум одна зона гарантированно останется
    здесь, это не баг скрипта."""
    matched = set(existing) | set(proposals)
    unmatched = [z for z in kb.catalog.zones if z.id not in matched]
    if unmatched:
        print(f"\nБез пары ({len(unmatched)}) — сотрудника не нашлось совсем:")
        for zone in unmatched:
            print(f"  {zone.id} — {zone.name}")
    else:
        print("\nБез пары зон не осталось.")

    conflicts = _find_duplicate_staff_assignments(proposals)
    if conflicts:
        print("\n⚠ ОДИН СОТРУДНИК ПРЕДЛОЖЕН СРАЗУ ДЛЯ НЕСКОЛЬКИХ ЗОН — это ошибка "
              "совпадения имён, не настоящая связка. Проверьте вручную:")
        for staff_id, zone_ids in conflicts.items():
            names = ", ".join(zone_ids)
            print(f"  staff_id={staff_id} -> {names}")


async def confirm_and_apply(
    kb: KnowledgeBase, proposals: dict[str, Candidate], mapping: SqlAlchemyZoneMapping,
) -> None:
    """service_id НЕ спрашивается автоматически (см. докстринг модуля —
    35 услуг это зона×длительность×день, не 1:1 с зоной) и не перезаписывается
    пустым: если оператор ничего не ввёл, ключ просто не передаётся в
    mapping.set(), а не затирается на None — иначе повторный прогон этого
    скрипта стирал бы service_id, заведённый отдельно в прошлый раз."""
    print("\nПодтверждение по каждой зоне (Enter — принять предложенное, "
          "или впишите свой staff_id, «n» — пропустить зону):")
    for zone in kb.catalog.zones:
        candidate = proposals.get(zone.id)
        if candidate is None:
            continue

        answer = _prompt(
            f"\n{zone.id} ({zone.name}) -> [{candidate.staff.staff_id}] "
            f"{candidate.staff.name}. Принять? [Y/n/staff_id]: "
        )
        if answer is None:
            print(f"  пропущено (нет интерактивного ввода): {zone.id}")
            continue
        if answer.lower() == "n":
            print(f"  пропущено: {zone.id}")
            continue
        staff_id = answer if answer and answer.lower() != "y" else candidate.staff.staff_id

        values: dict[str, str] = {"staff_id": staff_id}

        service_id = _prompt(
            "  service_id для брони (необязательно сейчас — см. таблицу услуг "
            "выше; используется при создании брони, не при проверке занятости; "
            "Enter — пропустить): "
        )
        if service_id:
            values["service_id"] = service_id

        company_id = _prompt(
            f"  company_id (Enter — взять из настроек, {get_settings().yclients_company_id or 'не задан'}): "
        )
        values["company_id"] = company_id or get_settings().yclients_company_id or None

        await mapping.set(zone.id, **values)
        print(f"  записано: {zone.id} -> staff_id={staff_id}"
              f"{', service_id=' + service_id if service_id else ' (service_id пока не задан)'}")


async def verify_live(provider: YClientsProvider, kb: KnowledgeBase, mapping: SqlAlchemyZoneMapping,
                       check_date: DateType) -> None:
    """Пункт 4 общей задачи: реальный check_availability на только что
    записанных зонах. UNKNOWN здесь — не абстрактный риск, а сигнал «что-то
    из только что введённого не так», и его нужно увидеть сразу, а не после
    жалобы клиента. check_availability уже спрашивает занятость по staff_id
    (см. app/booking/yclients.py:get_free_slots, BOOK_TIMES) — эта функция
    только показывает результат, ничего не меняя в самом запросе."""
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
                  f"Связка не работает, проверьте staff_id (и company_id).")
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

    # Пункт 1: полный список сотрудников (= зон) — до какого-либо матчинга.
    staff_list = await provider.get_staff()
    print_staff_table(staff_list)
    if not staff_list:
        print("\nСписок сотрудников пуст — проверьте scripts/inspect_yclients.py "
              "(раздел staff) и права токена на STAFF_FULL_LIST_DEPRECATED.")
        return 1

    # Пункт 3: полный список услуг — отдельно, только для обзора человеком.
    services = await provider.get_services()
    print_services_table(services)

    existing = {zid: zone_mapping.get(zid) for zid in zone_mapping.mapped_zones()}
    if existing:
        summary = ", ".join(f"{zid}(staff_id={row.get('staff_id')})" for zid, row in sorted(existing.items()))
        print(f"\nУже связаны ({len(existing)}): {summary}")

    proposals = propose_mapping(kb, staff_list, existing)
    print_unmatched_and_conflicts(kb, proposals, existing)

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
