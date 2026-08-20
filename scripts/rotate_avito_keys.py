"""Смена ключей Авито без даунтайма.

    python -m scripts.rotate_avito_keys --check
    python -m scripts.rotate_avito_keys --new-id ID --new-secret SECRET

Порядок важен: сначала проверяем, что новая пара вообще выдаёт токен, и только
потом сбрасываем кеш. Обратный порядок оставляет систему без рабочего токена
на время, пока кто-то ищет опечатку в новом secret.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.channels.avito import TOKEN_REDIS_KEY, AvitoAuth
from app.config import Settings, get_settings
from app.logging_setup import configure_logging


async def _can_get_token(client_id: str, client_secret: str) -> bool:
    probe = Settings(
        avito_client_id=client_id,
        avito_client_secret=client_secret,
        avito_user_id=get_settings().avito_user_id,
    )
    try:
        token = await AvitoAuth(probe).get_token(force_refresh=True)
        return bool(token)
    except Exception as exc:  # noqa: BLE001
        print(f"Новая пара ключей не работает: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False


async def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    configure_logging()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="проверить текущие ключи")
    parser.add_argument("--new-id")
    parser.add_argument("--new-secret")
    args = parser.parse_args()

    settings = get_settings()

    if args.check:
        client_id, client_secret = settings.require_avito_credentials()
        ok = await _can_get_token(client_id, client_secret)
        print("Текущие ключи рабочие." if ok else "Текущие ключи НЕ работают.")
        return 0 if ok else 1

    if not (args.new_id and args.new_secret):
        parser.print_help()
        return 2

    print("1/3 Проверяю новую пару ключей…")
    if not await _can_get_token(args.new_id, args.new_secret):
        print("Ротация отменена, старые ключи остались в силе.", file=sys.stderr)
        return 1

    print("2/3 Новая пара рабочая. Обновите .env:")
    print(f"    AVITO_CLIENT_ID={args.new_id}")
    print("    AVITO_CLIENT_SECRET=<новый secret>")
    print("   (значение secret намеренно не печатается в консоль)")

    print("3/3 После правки .env перезапустите приложение и сбросьте кеш токена:")
    print(f"    redis-cli DEL {TOKEN_REDIS_KEY}")
    print("\nСтарый токен продолжит работать до истечения, простоя не будет.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
