"""Подписка на колбэки Авито.

    python -m scripts.register_webhook https://example.ru
    python -m scripts.register_webhook --list
    python -m scripts.register_webhook --unsubscribe https://example.ru

Аргумент — ПУБЛИЧНАЯ БАЗА сайта, без пути. Секретный сегмент подставляется
из AVITO_WEBHOOK_SECRET автоматически, чтобы секрет не попал в историю
команд оболочки.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import httpx

from app.channels import avito_endpoints as ep
from app.channels.avito import AvitoAuth
from app.config import get_settings
from app.webhooks import webhook_path


async def _call(spec: tuple[str, str], payload: dict) -> dict:
    settings = get_settings()
    token = await AvitoAuth(settings).get_token()
    method, path = spec

    async with httpx.AsyncClient(
        base_url=ep.BASE_URL, timeout=settings.avito_timeout_seconds
    ) as client:
        response = await client.request(
            method,
            path,
            headers={ep.AUTH_HEADER: f"{ep.AUTH_SCHEME} {token}"},
            json=payload,
        )
        response.raise_for_status()
        return response.json()


def _full_url(base: str) -> str:
    secret = get_settings().require_webhook_secret()
    return base.rstrip("/") + webhook_path(secret)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", nargs="?", help="публичная база, например https://example.ru")
    parser.add_argument("--list", action="store_true", help="показать активные подписки")
    parser.add_argument("--unsubscribe", metavar="BASE_URL", help="отключить уведомления")
    args = parser.parse_args()

    try:
        if args.list:
            print(await _call(ep.LIST_SUBSCRIPTIONS, {}))
        elif args.unsubscribe:
            url = _full_url(args.unsubscribe)
            print(await _call(ep.WEBHOOK_UNSUBSCRIBE, {"url": url}))
        elif args.base_url:
            if not args.base_url.startswith("https://"):
                print("Авито требует HTTPS для вебхука", file=sys.stderr)
                return 2
            url = _full_url(args.base_url)
            result = await _call(ep.WEBHOOK_SUBSCRIBE, {"url": url})
            # Секрет в консоль не печатаем.
            print(f"Подписка зарегистрирована на {args.base_url.rstrip('/')}/webhook/avito/***")
            print(result)
        else:
            parser.print_help()
            return 2
    except RuntimeError as exc:
        print(f"Отказано: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
