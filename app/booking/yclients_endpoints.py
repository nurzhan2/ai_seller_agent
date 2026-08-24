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

# НЕ ПОДТВЕРЖДЕНО — заказчик подтвердил остальное, но не это. Оплата остаётся
# за оператором; это не блокирует запуск (агенту и так запрещено вести оплату).
PAYMENT_LINK: Final[tuple[str, str]] = ("POST", "/payment/link/{company_id}")
PAYMENT_LINK_SUPPORTED: Final[bool] = False

# HTTP-статусы, которыми YCLIENTS отдаёт «интеграция не подключена филиалом»
# (в отличие от отказа домена/сети/500) — см. app/booking/yclients.py.
# Заказчик описал это как «ошибка доступа», без точного кода; 401/403 —
# стандартная пара для «валидные токены, но нет прав» в REST API, которую и
# стоит отличать от прочих сбоев в логе. Если реальный код на проде окажется
# другим — здесь единственное место, которое нужно поправить.
INTEGRATION_NOT_CONNECTED_STATUSES: Final[tuple[int, ...]] = (401, 403)

# Ответ v2.0 всегда содержит success / data / meta.
RESPONSE_ENVELOPE: Final[tuple[str, str, str]] = ("success", "data", "meta")


class SpecNotVerifiedError(RuntimeError):
    """Поднимается вместо запроса, собранного из неподтверждённых догадок."""


def assert_spec_verified() -> None:
    if not SPEC_VERIFIED:
        raise SpecNotVerifiedError(
            "Схема YCLIENTS не подтверждена. См. app/booking/yclients_endpoints.py"
        )
