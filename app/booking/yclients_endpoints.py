"""Спецификация YCLIENTS API — НЕ ПОДТВЕРЖДЕНА.

⚠️ SPEC_VERIFIED = False

https://developers.yclients.com/ru/ не удалось загрузить из этой среды
(страница отдаётся усечённой), а доступные вторичные источники расходятся в
том, что нельзя угадывать:

  * БАЗОВЫЙ URL — часть обёрток использует `https://api.yclients.com/api/v1`,
    другая часть шардированный `https://n{N}.yclients.com/api/v1`, где N —
    номер, зависящий от компании. Ошибка здесь означает 100% отказов.
  * ЗАГОЛОВОК АВТОРИЗАЦИИ — встречается формат
    `Authorization: Bearer <partner_token>, User <user_token>`, то есть два
    токена в одном заголовке через запятую. Это нетипично, и точный вид
    (пробелы, регистр, порядок) нужно подтвердить.
  * ССЫЛКА НА ОПЛАТУ — существует ли отдельный эндпоинт генерации платёжной
    ссылки, вообще не подтверждено. Возможно, оплата настраивается на стороне
    компании и ссылка приходит другим путём.

ЧТО ДЕЛАТЬ
    1. Открыть https://developers.yclients.com/ru/ и заполнить константы с
       пометкой VERIFY.
    2. Уточнить у заказчика номер шарда (или подтвердить общий домен).
    3. Выставить SPEC_VERIFIED = True.
    4. Прогнать tests/test_booking.py — тесты написаны path-agnostic и должны
       пройти без изменений.

Пока флаг False, `YClientsProvider` не делает ни одного сетевого запроса и
честно возвращает UNKNOWN. Агент на UNKNOWN говорит «уточню у менеджера» и
эскалирует — то есть система деградирует, а не врёт клиенту о свободных
датах.
"""

from __future__ import annotations

from typing import Final

SPEC_VERIFIED: Final[bool] = False

# VERIFY: общий домен или шардированный n{N}?
BASE_URL: Final[str] = "https://api.yclients.com/api/v1"

# VERIFY: точный формат совмещённого заголовка двух токенов.
AUTH_HEADER: Final[str] = "Authorization"
AUTH_TEMPLATE: Final[str] = "Bearer {partner_token}, User {user_token}"
ACCEPT_HEADER: Final[str] = "application/vnd.yclients.v2+json"

# VERIFY: пути и имена параметров.
SERVICES: Final[tuple[str, str]] = ("GET", "/book_services/{company_id}")
BOOK_DATES: Final[tuple[str, str]] = ("GET", "/book_dates/{company_id}")
BOOK_TIMES: Final[tuple[str, str]] = ("GET", "/book_times/{company_id}/{staff_id}/{date}")
BOOK_RECORD: Final[tuple[str, str]] = ("POST", "/book_record/{company_id}")
DELETE_RECORD: Final[tuple[str, str]] = ("DELETE", "/user/records/{record_id}")

# VERIFY: существует ли вообще. Если нет — этап оплаты остаётся за оператором,
# и это не блокирует запуск: агенту и так запрещено вести оплату.
PAYMENT_LINK: Final[tuple[str, str]] = ("POST", "/payment/link/{company_id}")
PAYMENT_LINK_SUPPORTED: Final[bool] = False

# Ответ v2.0 всегда содержит success / data / meta.
RESPONSE_ENVELOPE: Final[tuple[str, str, str]] = ("success", "data", "meta")


class SpecNotVerifiedError(RuntimeError):
    """Поднимается вместо запроса, собранного из неподтверждённых догадок."""


def assert_spec_verified() -> None:
    if not SPEC_VERIFIED:
        raise SpecNotVerifiedError(
            "Схема YCLIENTS не подтверждена. См. app/booking/yclients_endpoints.py"
        )
