"""Диагностика получения токена Авито: что именно отвергает авторизация.

    python -m scripts.diagnose_avito_token
    railway run python -m scripts.diagnose_avito_token     # с боевыми ключами

Только чтение: ничего не отправляет клиентам, ничего не пишет в базу и в
каталог. Единственное сетевое действие — попытки получить токен.

ЗАЧЕМ ОТДЕЛЬНЫЙ СКРИПТ. `unauthorized_client` от Авито не различает «ключи
не те», «приложению не разрешён этот grant», «неверный способ передачи
параметров» и «интеграция не активирована в кабинете». Матрица ниже
разносит эти случаи по разным ответам: например, JSON в теле отвечает
`unsupported_grant_type` вместо `unauthorized_client` — то есть эндпоинт
тело разбирает и транспорт различает, и если form и query дают ОДНУ ошибку,
то дело не в способе передачи.

СЕКРЕТЫ НЕ ПЕЧАТАЮТСЯ. Значения маскируются в URL, в теле запроса и в теле
ответа. Печатаются длина и характер краёв строки — этого хватает, чтобы
поймать лишний перевод строки, кавычки или плейсхолдер из .env.example,
приехавшие вместе с переменной окружения (на этом уже горели: локальный
.env содержал дословное `your_client_id_here`).
"""

from __future__ import annotations

import asyncio
import base64
import re
import sys
from typing import Any, Optional

import httpx

from app.config import get_settings

BASE_URL = "https://api.avito.ru"
TIMEOUT = 20.0

# Плейсхолдеры из .env.example: если переменная равна одному из них, дальше
# можно не гадать — клиента с таким id у Авито нет и быть не может.
PLACEHOLDERS = (
    "your_client_id_here",
    "your_client_secret_here",
    "your_avito_user_id_here",
)


def describe(name: str, value: Optional[str]) -> str:
    """Характер значения без самого значения."""
    if not value:
        return f"{name}: НЕ ЗАДАН"
    raw = repr(value)
    flags = []
    if value in PLACEHOLDERS:
        flags.append("!!! ПЛЕЙСХОЛДЕР ИЗ .env.example")
    if any(c.isspace() for c in value):
        flags.append("!!! содержит пробельные символы")
    if any(c in value for c in "\"'"):
        flags.append("!!! содержит кавычки")
    if not value.isascii():
        flags.append("!!! не только ASCII")
    tail = ("  " + "; ".join(flags)) if flags else ""
    return (
        f"{name}: длина={len(value)}, начало={raw[1:3]}, конец={raw[-3:-1]}{tail}"
    )


# Выданный токен — тоже секрет, и в этом скрипте он опаснее ключей: его
# вывод несут в поддержку Авито и вставляют в переписку. Токен живёт сутки,
# и этого более чем достаточно, чтобы им воспользовался кто угодно.
_TOKEN_RE = re.compile(r'("access_token"\s*:\s*")([^"]+)(")')


def mask(text: str, secrets: tuple[str, ...]) -> str:
    if not text:
        return text
    for value, label in zip(secrets, ("<CLIENT_ID>", "<CLIENT_SECRET>")):
        if value:
            text = text.replace(value, label)
    # Имя поля остаётся видимым — по нему и понятно, что токен выдан.
    return _TOKEN_RE.sub(r"\1<ACCESS_TOKEN>\3", text)


async def probe(client: httpx.AsyncClient, label: str, secrets: tuple[str, ...],
                url: str, **kwargs: Any) -> Optional[dict]:
    print("=" * 78)
    print(f"ПОПЫТКА: {label}")
    print("-" * 78)
    request = client.build_request("POST", url, **kwargs)
    print(f"  {request.method} {mask(str(request.url), secrets)}")
    print("  ЗАГОЛОВКИ ЗАПРОСА:")
    for key, value in request.headers.items():
        shown = "<замаскировано>" if key.lower() == "authorization" else value
        print(f"    {key}: {shown}")
    body = request.content.decode("utf-8", "replace")
    print(f"  ТЕЛО ЗАПРОСА: {mask(body, secrets) if body else '(пусто)'}")

    try:
        response = await client.send(request)
    except Exception as exc:  # noqa: BLE001
        print(f"  ОТВЕТ: транспортная ошибка {type(exc).__name__}: {exc}")
        return None

    print(f"  HTTP {response.status_code} {response.reason_phrase}")
    print("  ЗАГОЛОВКИ ОТВЕТА:")
    for key, value in response.headers.items():
        print(f"    {key}: {value}")
    print(f"  ТЕЛО ОТВЕТА ЦЕЛИКОМ ({len(response.text)} байт):")
    print(f"    {mask(response.text, secrets)}")

    try:
        payload = response.json()
    except Exception:  # noqa: BLE001
        print("  (тело не JSON)")
        return None
    if isinstance(payload, dict):
        print(f"  ПОЛЯ JSON: {sorted(payload)}")
        if "access_token" in payload:
            print("  >>> ТОКЕН ПОЛУЧЕН <<<")
        return payload
    return None


async def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    settings = get_settings()
    client_id, client_secret = settings.require_avito_credentials()
    secrets = (client_id, client_secret)

    print("ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ (значения не печатаются):")
    print("  " + describe("AVITO_CLIENT_ID", client_id))
    print("  " + describe("AVITO_CLIENT_SECRET", client_secret))
    print("  " + describe("AVITO_USER_ID", settings.avito_user_id))
    print()

    if client_id in PLACEHOLDERS or client_secret in PLACEHOLDERS:
        print("ВЫВОД БЕЗ ЗАПРОСОВ: в окружении лежат плейсхолдеры из "
              ".env.example, а не ключи. Любой ответ Авито будет "
              "`unauthorized_client`, и он не про формат запроса.")
        print("Запускать эту диагностику нужно там, где заданы настоящие "
              "ключи: `railway run python -m scripts.diagnose_avito_token`.")
        return 2

    form = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    attempts = [
        # Первым — ровно то, что делает боевой код (app/channels/avito.py).
        ("1. query string, /token (ТЕКУЩИЙ БОЕВОЙ КОД)",
         f"{BASE_URL}/token", {"params": form}),
        ("2. form-urlencoded в теле, /token (как в документации)",
         f"{BASE_URL}/token", {"data": form}),
        ("3. form-urlencoded в теле, /token/ (слеш на конце)",
         f"{BASE_URL}/token/", {"data": form}),
        ("4. query string, /token/ (слеш на конце)",
         f"{BASE_URL}/token/", {"params": form}),
        # Другая ошибка здесь означает, что тело разбирается и транспорт
        # различается — то есть form/query отвергаются НЕ из-за формата.
        ("5. JSON в теле, /token",
         f"{BASE_URL}/token", {"json": form}),
        ("6. form-urlencoded + явный Content-Type, /token",
         f"{BASE_URL}/token",
         {"data": form,
          "headers": {"Content-Type": "application/x-www-form-urlencoded",
                      "Accept": "application/json"}}),
        ("7. form-urlencoded + scope, /token",
         f"{BASE_URL}/token",
         {"data": {**form, "scope": "messenger:read,messenger:write"}}),
        ("8. HTTP Basic + grant_type в теле, /token",
         f"{BASE_URL}/token",
         {"data": {"grant_type": "client_credentials"},
          "headers": {"Authorization": f"Basic {basic}"}}),
        ("9. form-urlencoded, /oauth2/token (второй документированный путь)",
         f"{BASE_URL}/oauth2/token", {"data": form}),
    ]

    got_token = False
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for label, url, kwargs in attempts:
            payload = await probe(client, label, secrets, url, **kwargs)
            if payload and "access_token" in payload:
                got_token = True

    print("=" * 78)
    if got_token:
        print("ИТОГ: токен выдаётся. Дальше — scripts/import_photos.py и "
              "остальные вызовы API.")
        return 0
    print("ИТОГ: токен не выдал ни один вариант. Если все ответы одинаковы "
          "(`unauthorized_client`), а JSON-вариант отвечает иначе — дело не "
          "в способе передачи параметров, а в самом приложении Авито: "
          "проверьте, что ключи от ТОГО аккаунта и что интеграция "
          "активирована в кабинете продавца. С этим выводом и полными "
          "телами ответов выше можно идти в поддержку Авито.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
