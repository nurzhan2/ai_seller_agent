"""Только чтение: показать, что реально отвечает YCLIENTS по каждому разделу.

    python -m scripts.inspect_yclients

НИЧЕГО НЕ МЕНЯЕТ — только GET-запросы, ни одного POST/PATCH/PUT/DELETE.

ПОЧЕМУ ЭТОТ СКРИПТ СУЩЕСТВУЕТ. scripts/sync_yclients_services.py читает
book_services и получает 0 услуг, хотя заказчик показал скриншот личного
кабинета: расписание работы заведено для всех 10 зон. Отсюда две разные
гипотезы, и обе стоит проверить, а не гадать заранее:

  1. Зоны заведены как ресурсы или сотрудники, а не как услуги, доступные
     для онлайн-записи (см. app/booking/yclients_endpoints.py:RESOURCES,
     STAFF_FULL_LIST) — тогда book_services пуст ЗАКОНОМЕРНО, потому что
     это не про них.
  2. У части этих зон-услуг может быть выключен флаг доступности для
     онлайн-записи (в терминах официальной документации YCLIENTS — поле
     service_type/is_online, см. SERVICES_FULL_LIST_DEPRECATED) — тогда
     они существуют как услуги, но book_services их не покажет по
     конструкции метода.

Пока писался этот скрипт, нашлась и третья, более прозаичная причина —
баг разбора ответа в app/booking/yclients.py: `get_services()` ждал от
book_services плоский список, а по официальной документации
(developers.yclients.com, раздел "Онлайн-запись" -> "Получить список
услуг доступных для бронирования") data — это ОБЪЕКТ вида
`{"categories": [...], "services": [...]}`. При любом настоящем ответе
`isinstance(data, list)` был False, и `get_services()` молча возвращал
`[]` — то есть 0 могло получаться ДАЖЕ если у заказчика всё заведено
правильно. Этот баг исправлен отдельно в app/booking/yclients.py; данный
скрипт не полагается на YClientsProvider.get_services() именно поэтому —
здесь читается сырой ответ API напрямую, чтобы диагностика не зависела ни
от одной из двух версий парсинга.

ПУТИ ИЗ ДОКУМЕНТАЦИИ, НЕ ОТ ЗАКАЗЧИКА. base_url/заголовки/конверт
(app/booking/yclients_endpoints.py:SPEC_VERIFIED) заказчик подтвердил
напрямую. Пути ниже (SERVICES_FULL_LIST, STAFF_FULL_LIST, RESOURCES,
BOOK_STAFF, ONLINE_SETTINGS, BOOKING_FORMS) сняты с официальной
документации https://developers.yclients.com/ru/ (Redoc, разобрано
постранично через DOM 2026-08-26 — сама страница не отдаёт OpenAPI-файл
напрямую) и ещё НЕ проверены на данных этого заказчика. Проверка на живых
данных — единственная цель этого скрипта.

service_id/staff_id в "полном каталоге" документированы как обязательные
path-параметры, но заголовок операции в документации — "Получить список
услуг / КОНКРЕТНУЮ услугу" (аналогично для сотрудников): один документный
узел явно описывает два реальных REST-маршрута — коллекцию и элемент.
Поэтому здесь бьём в путь БЕЗ id (предполагаемая коллекция) и просто
смотрим на реальный код ответа — 404 тоже результат, и скрипт его не
скрывает.

Лимит YCLIENTS — 200 запросов в минуту / 5 в секунду с одного IP (см.
документацию, раздел INTRODUCTION); проверок здесь на порядок меньше,
но между запросами всё равно есть небольшая пауза.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.booking import yclients_endpoints as ep
from app.config import get_settings
from app.logging_setup import configure_logging

REQUEST_DELAY_SECONDS = 0.3


@dataclass(frozen=True)
class Check:
    label: str
    path: str
    params: Optional[dict] = None


def _build_checks(company_id: str) -> list[Check]:
    return [
        Check(
            "book_services (то, что сейчас читает YClientsProvider.get_services)",
            ep.SERVICES[1].format(company_id=company_id),
        ),
        Check(
            "services — полный каталог, новый метод (без id -> предполагаемая коллекция)",
            ep.SERVICES_FULL_LIST[1].format(company_id=company_id),
        ),
        Check(
            "services — полный каталог, устаревший метод (в документации есть поле is_online)",
            ep.SERVICES_FULL_LIST_DEPRECATED[1].format(company_id=company_id),
        ),
        Check(
            "staff — сотрудники, новый метод (без id -> предполагаемая коллекция)",
            ep.STAFF_FULL_LIST[1].format(company_id=company_id),
        ),
        Check(
            "staff — сотрудники, устаревший метод",
            ep.STAFF_FULL_LIST_DEPRECATED[1].format(company_id=company_id),
        ),
        Check(
            "resources — ресурсы (переговорочные, подъёмники и т.п.)",
            ep.RESOURCES[1].format(company_id=company_id),
        ),
        Check(
            "book_staff — сотрудники, доступные для онлайн-записи",
            ep.BOOK_STAFF[1].format(company_id=company_id),
        ),
        Check(
            "book_dates — доступные даты для онлайн-записи (без фильтра по услуге/сотруднику)",
            ep.BOOK_DATES[1].format(company_id=company_id),
        ),
        Check(
            "settings/online — настройки онлайн-записи (НЕ рубильник вкл/выкл, см. докстринг)",
            ep.ONLINE_SETTINGS[1].format(company_id=company_id),
        ),
        Check(
            "booking_forms — виджеты онлайн-записи (пусто = запись нигде не подключена)",
            ep.BOOKING_FORMS[1].format(company_id=company_id),
        ),
    ]


def _describe_objects(items: list) -> str:
    if not items:
        return "0 объектов"
    lines = [f"{len(items)} объект(ов), первые {min(3, len(items))}:"]
    for item in items[:3]:
        if isinstance(item, dict):
            ident = item.get("id", "?")
            name = item.get("title") or item.get("name") or "?"
            extra = ""
            if "is_online" in item:
                extra += f" is_online={item['is_online']}"
            if "service_type" in item:
                extra += f" service_type={item['service_type']}"
            lines.append(f"    id={ident!r} title/name={name!r}{extra}")
        else:
            lines.append(f"    {item!r}")
    return "\n".join(lines)


def _summarize_data(data: Any) -> str:
    if data is None:
        return "  data пуст (null)"
    if isinstance(data, list):
        return "  " + _describe_objects(data)
    if isinstance(data, dict):
        list_keys = [k for k, v in data.items() if isinstance(v, list)]
        if list_keys:
            return "\n".join(f"  [{k}] {_describe_objects(data[k])}" for k in list_keys)
        # Одиночный объект (например, настройки) — не список, показываем как есть.
        dumped = json.dumps(data, ensure_ascii=False)
        return f"  объект (не список): {dumped[:600]}"
    return f"  неожиданный тип data: {type(data).__name__}: {str(data)[:300]!r}"


async def run_check(client: httpx.AsyncClient, headers: dict[str, str], check: Check) -> None:
    url = ep.BASE_URL + check.path
    print(f"\n=== {check.label} ===")
    print(f"GET {url}" + (f"  params={check.params}" if check.params else ""))

    try:
        response = await client.get(check.path, headers=headers, params=check.params)
    except Exception as exc:  # noqa: BLE001 — диагностика: любой сбой должен быть виден, не проглочен
        print(f"ОШИБКА СЕТИ: {type(exc).__name__}: {exc}")
        return

    print(f"код ответа: {response.status_code}")

    if response.status_code in ep.ACCESS_DENIED_STATUSES:
        print(
            "  -> 401/403: либо интеграция не подключена филиалом (тогда откажут "
            "ВСЕ методы), либо у токена просто нет прав на этот конкретный метод "
            "(тогда часть методов выше могла отвечать 200) — сравните с другими "
            "проверками в этом прогоне, не считайте одну причину доказанной."
        )

    try:
        payload = response.json()
    except ValueError:
        print(f"тело не JSON: {response.text[:300]!r}")
        return

    if not isinstance(payload, dict):
        print(f"неожиданное тело (не объект): {str(payload)[:400]!r}")
        return

    success = payload.get("success")
    meta = payload.get("meta")
    print(f"success={success}  meta={meta}")

    if success is False:
        print(f"ОШИБКА ОТ YCLIENTS: {meta}")
        return

    print(_summarize_data(payload.get("data")))


async def check_slots(zone_id: str, day: str) -> int:
    """Живая проверка занятости ОДНОЙ зоны на одну дату: сырой ответ
    book_times и то, во что его превратил наш разбор.

    Существует потому, что «HTTP 200, а занятость unknown» иначе
    приходится выяснять по логам прода: здесь обе стороны видно рядом —
    что реально прислал YCLIENTS и что из этого получил агент.
    """
    from datetime import date as _date

    from app.booking.mapping import SqlAlchemyZoneMapping
    from app.booking.yclients import YClientsProvider
    from app.db.session import get_sessionmaker

    settings = get_settings()
    try:
        booking_day = _date.fromisoformat(day)
    except ValueError:
        print(f"Дата {day!r} не в формате YYYY-MM-DD.", file=sys.stderr)
        return 1

    mapping = SqlAlchemyZoneMapping(get_sessionmaker())
    await mapping.load()
    row = mapping.get(zone_id)
    if row is None:
        print(f"Зона {zone_id} не связана в zone_service_map — занятость спрашивать не у кого.\n"
              "Связать: python -m scripts.sync_yclients_services --apply", file=sys.stderr)
        return 1
    print(f"zone={zone_id}  staff_id={row.get('staff_id')}  "
          f"company_id={row.get('company_id') or settings.yclients_company_id}  date={day}")

    headers = {
        ep.AUTH_HEADER: ep.AUTH_TEMPLATE.format(
            partner_token=settings.yclients_partner_token.get_secret_value(),
            user_token=settings.yclients_user_token.get_secret_value(),
        ),
        "Accept": ep.ACCEPT_HEADER,
    }
    path = ep.BOOK_TIMES[1].format(
        company_id=row.get("company_id") or settings.yclients_company_id,
        staff_id=row.get("staff_id", "0"),
        date=booking_day.isoformat(),
    )
    async with httpx.AsyncClient(base_url=ep.BASE_URL, timeout=15.0) as client:
        await run_check(client, headers, Check("book_times — сырой ответ", path))

    provider = YClientsProvider(
        partner_token=settings.yclients_partner_token.get_secret_value(),
        user_token=settings.yclients_user_token.get_secret_value(),
        company_id=settings.yclients_company_id,
        mapping=mapping,
    )
    availability = await provider.check_availability(zone_id, booking_day)
    print(f"\nНаш разбор: {availability.status.value.upper()}")
    print(f"  свободные времена: {', '.join(availability.free_slots) or '—'}")
    print(f"  причина: {availability.reason or '—'}")
    if availability.status.value == "unknown":
        print("\n  UNKNOWN означает, что ответ не удалось разобрать или запрос не удался —\n"
              "  сырой ответ выше показывает, что именно пришло.")
    return 0


async def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    configure_logging()

    # Разбор аргументов ДО проверки токенов: иначе `--help` не работает без
    # настроенного окружения, а это первое, что запускают, чтобы вспомнить
    # синтаксис.
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--slots-zone", help="проверить занятость одной зоны, напр. bath_russian")
    parser.add_argument("--slots-date", help="дата для --slots-zone, YYYY-MM-DD")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.yclients_partner_token.get_secret_value():
        print("YCLIENTS_PARTNER_TOKEN не задан — нечего инспектировать.", file=sys.stderr)
        return 1
    if not settings.yclients_company_id:
        print("YCLIENTS_COMPANY_ID не задан — нечего инспектировать.", file=sys.stderr)
        return 1

    if args.slots_zone:
        if not args.slots_date:
            print("--slots-zone требует --slots-date YYYY-MM-DD", file=sys.stderr)
            return 1
        return await check_slots(args.slots_zone, args.slots_date)

    headers = {
        ep.AUTH_HEADER: ep.AUTH_TEMPLATE.format(
            partner_token=settings.yclients_partner_token.get_secret_value(),
            user_token=settings.yclients_user_token.get_secret_value(),
        ),
        "Accept": ep.ACCEPT_HEADER,
    }
    checks = _build_checks(settings.yclients_company_id)

    print(f"company_id={settings.yclients_company_id}  base_url={ep.BASE_URL}")
    print(f"Проверок: {len(checks)}. Только GET, ничего не меняется.")

    async with httpx.AsyncClient(base_url=ep.BASE_URL, timeout=15.0) as client:
        for i, check in enumerate(checks):
            await run_check(client, headers, check)
            if i < len(checks) - 1:
                await asyncio.sleep(REQUEST_DELAY_SECONDS)

    print("\nГотово. Это чтение — ни одна запись/услуга/сотрудник не были изменены.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
