"""Спецификация YCLIENTS API.

✅ SPEC_VERIFIED = True — подтверждено заказчиком после получения ключей
(2026-08-24), не угадано. Раньше здесь было три открытых развилки
(общий домен vs шардированный, точный вид заголовка авторизации,
существование эндпоинта оплаты) — сняты его прямым подтверждением:

  * base_url: `https://api.yclients.com/api/v1` (общий домен, не шардированный)
  * заголовок авторизации — оба токена в ОДНОМ заголовке `Authorization`,
    через запятую: `Bearer <partner_token>, User <user_token>`
  * `Accept: application/vnd.yclients.v2+json`
  * ответ v2.0 — всегда конверт `{success, data, meta}`; проверять именно
    `success`, а не только HTTP-код (200 с `success: false` — тоже отказ)
  * company_id — не в заголовках, подставляется в путь; значение берётся из
    конфига (`Settings.yclients_company_id`), а не запрашивается каждый раз

Ссылка на оплату (`PAYMENT_LINK`) по-прежнему НЕ подтверждена — заказчик
подтвердил только то, что перечислено выше. `PAYMENT_LINK_SUPPORTED` остаётся
`False`, путь ниже — черновик на будущее, не для использования.

ДВЕ ВЕЩИ, КОТОРЫЕ НЕ БАГ, А ОЖИДАЕМОЕ СОСТОЯНИЕ ПОСЛЕ ЭТОГО ФЛАГА:

  * Интеграция подключается филиалом вручную в личном кабинете YCLIENTS.
    Пока филиал не нажал «Подключить», запросы с формально верными токенами
    всё равно вернут ошибку доступа (типично — HTTP 401/403, иногда
    `success: false` в теле 200-го ответа). Это не «код сломан» и не повод
    откатывать SPEC_VERIFIED — `YClientsProvider._request` (app/booking/
    yclients.py) отдельно ловит именно этот случай и пишет в лог понятное
    сообщение, а не общее «request failed», чтобы дежурный не тратил время
    на разбор кода. Наружу — как всегда, UNKNOWN, никогда не выдуманное FREE.
  * Каталог услуг у заказчика пока пуст — `get_services()` вернёт `[]`.
    Это тоже ожидаемо, не ошибка (см. промт про YCLIENTS, часть 8 — заказчику
    просили не «чинить» неполный каталог, а показывать его состояние как
    есть, см. app/booking/mapping.py:coverage_report).

Прогнать после изменений: `pytest tests/test_booking.py` — тесты написаны
path-agnostic и должны пройти без изменений.
"""

from __future__ import annotations

from typing import Final

SPEC_VERIFIED: Final[bool] = True

BASE_URL: Final[str] = "https://api.yclients.com/api/v1"

AUTH_HEADER: Final[str] = "Authorization"
AUTH_TEMPLATE: Final[str] = "Bearer {partner_token}, User {user_token}"
ACCEPT_HEADER: Final[str] = "application/vnd.yclients.v2+json"

SERVICES: Final[tuple[str, str]] = ("GET", "/book_services/{company_id}")
BOOK_DATES: Final[tuple[str, str]] = ("GET", "/book_dates/{company_id}")
BOOK_TIMES: Final[tuple[str, str]] = ("GET", "/book_times/{company_id}/{staff_id}/{date}")
BOOK_RECORD: Final[tuple[str, str]] = ("POST", "/book_record/{company_id}")
DELETE_RECORD: Final[tuple[str, str]] = ("DELETE", "/user/records/{record_id}")

# --------------------------------------------------------------------------
# Пути — из официальной документации https://developers.yclients.com/ru/
# (Redoc, снято 2026-08-26 постраничным разбором отрендеренного DOM — сама
# страница не даёт скачать OpenAPI-файл напрямую). РЕАЛЬНОЕ ПОВЕДЕНИЕ на
# токене этого заказчика — из живого прогона scripts/inspect_yclients.py
# (2026-08-26): STAFF_FULL_LIST_DEPRECATED и SERVICES(book_services) → 200;
# STAFF_FULL_LIST (новый) и ONLINE_SETTINGS → 403 на ТОМ ЖЕ токене (см.
# ACCESS_DENIED_STATUSES ниже — это разные права на разные методы, не
# «интеграция не подключена»); RESOURCES → 200, но пусто (ресурсов у
# заказчика не заведено — гипотеза "зоны это ресурсы" не подтвердилась,
# зоны оказались сотрудниками, см. app/booking/base.py:Staff).
#
# service_id/staff_id в путях "полного каталога" документированы как
# "required" в таблице path-параметров, но заголовок операции в доке —
# "Получить список услуг / КОНКРЕТНУЮ услугу" (аналогично для сотрудников),
# т.е. один документный узел описывает СРАЗУ два реальных REST-маршрута:
# коллекцию и элемент. Пример ответа при этом — массив из нескольких
# объектов, что для по-настоящему обязательного id было бы бессмысленно.
# Поэтому *_LIST ниже — без id, предположительно коллекция.
SERVICES_FULL_LIST: Final[tuple[str, str]] = ("GET", "/company/{company_id}/services")
SERVICES_FULL_ITEM: Final[tuple[str, str]] = ("GET", "/company/{company_id}/services/{service_id}")
# Устаревший метод, но в примере ответа документации явно есть поле
# "is_online" (доступность услуги для онлайн-записи) — именно то, чего нет
# в новом методе выше. Раздел "Устаревшее. Получить список услуг...".
SERVICES_FULL_LIST_DEPRECATED: Final[tuple[str, str]] = ("GET", "/services/{company_id}")

# Живой прогон: 403 (нет прав у токена именно на этот метод — см. заметку
# выше). Не используется — оставлен как документированный путь на случай,
# если права токена расширят.
STAFF_FULL_LIST: Final[tuple[str, str]] = ("GET", "/company/{company_id}/staff")
STAFF_FULL_ITEM: Final[tuple[str, str]] = ("GET", "/company/{company_id}/staff/{staff_id}")
# Раздел "Устаревшее. Получить список сотрудников / конкретного сотрудника".
# Живой прогон: 200, реально используется — YClientsProvider.get_staff()
# (app/booking/yclients.py). У этого заказчика сотрудник = зона комплекса.
STAFF_FULL_LIST_DEPRECATED: Final[tuple[str, str]] = ("GET", "/staff/{company_id}")

# Раздел документации "Ресурсы". Живой прогон: 200, но пусто — у заказчика
# ресурсы не заведены вообще, зоны оказались сотрудниками (см. выше).
RESOURCES: Final[tuple[str, str]] = ("GET", "/resources/{company_id}")

# Раздел "Онлайн-запись" — список сотрудников, ДОСТУПНЫХ для онлайн-записи
# (в отличие от STAFF_FULL_LIST — общего списка сотрудников филиала).
BOOK_STAFF: Final[tuple[str, str]] = ("GET", "/book_staff/{company_id}")

# Раздел "Настройки онлайн-записи". Судя по документированному ответу
# (confirm_number/any_master/seance_delay_step/...) — это НЕ рубильник
# "включена ли онлайн-запись вообще", а тонкие настройки поведения формы.
# Прямого булева флага "онлайн-запись включена" в документации не найдено;
# BOOKING_FORMS ниже (пустой/непустой список форм) — более прямой признак.
# Живой прогон: 403 на этом токене (см. заметку про права выше) — не то же
# самое, что «не подключено», раз book_services/staff отвечают 200.
ONLINE_SETTINGS: Final[tuple[str, str]] = ("GET", "/company/{company_id}/settings/online")

# Раздел "Настройки букформы" — список виджетов онлайн-записи филиала.
# Путь в документации именно с конечным слэшем.
BOOKING_FORMS: Final[tuple[str, str]] = ("GET", "/company/{company_id}/booking_forms/")

# НЕ ПОДТВЕРЖДЕНО — заказчик подтвердил остальное, но не это. Оплата остаётся
# за оператором; это не блокирует запуск (агенту и так запрещено вести оплату).
PAYMENT_LINK: Final[tuple[str, str]] = ("POST", "/payment/link/{company_id}")
PAYMENT_LINK_SUPPORTED: Final[bool] = False

# HTTP-статусы отказа в доступе (в отличие от сбоя домена/сети/500) — см.
# app/booking/yclients.py:_request(). ВАЖНО, что здесь НЕ утверждается:
# 401/403 — это НЕ обязательно «филиал не подключил интеграцию». Разведка
# через scripts/inspect_yclients.py (2026-08-26) на одном и том же токене
# получила 200 на /staff/{company_id} и /book_services/{company_id}, но 403
# на /company/{company_id}/staff и /company/{company_id}/settings/online —
# т.е. у токена просто нет прав на КОНКРЕТНЫЙ метод, а не «интеграция не
# подключена вообще». Если бы это было «не подключено», отказ был бы на
# ВСЕХ методах разом, а не выборочно. Поэтому лог в _request() формулирует
# обе причины, а не утверждает первую как единственную.
ACCESS_DENIED_STATUSES: Final[tuple[int, ...]] = (401, 403)

# Ответ v2.0 всегда содержит success / data / meta.
RESPONSE_ENVELOPE: Final[tuple[str, str, str]] = ("success", "data", "meta")


class SpecNotVerifiedError(RuntimeError):
    """Поднимается вместо запроса, собранного из неподтверждённых догадок."""


def assert_spec_verified() -> None:
    if not SPEC_VERIFIED:
        raise SpecNotVerifiedError(
            "Схема YCLIENTS не подтверждена. См. app/booking/yclients_endpoints.py"
        )
