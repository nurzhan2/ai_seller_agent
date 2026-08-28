"""Подписка на колбэки Авито.

    python -m scripts.register_webhook https://example.ru
    python -m scripts.register_webhook --list
    python -m scripts.register_webhook --unsubscribe https://example.ru

Аргумент — ПУБЛИЧНАЯ БАЗА сайта, без пути. Секретный сегмент подставляется
из AVITO_WEBHOOK_SECRET автоматически, чтобы секрет не попал в историю
команд оболочки.

Сама логика запросов живёт в `app/channels/avito_webhook_admin.py`: с тех
пор как перепривязка делается ещё и по расписанию (раз в сутки, см.
app/main.py), у скрипта и у планировщика обязан быть один код.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.channels import avito_webhook_admin as admin


async def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", nargs="?", help="публичная база, например https://example.ru")
    parser.add_argument("--list", action="store_true", help="показать активные подписки")
    parser.add_argument("--unsubscribe", metavar="BASE_URL", help="отключить уведомления")
    args = parser.parse_args()

    try:
        if args.list:
            print(await admin.list_subscriptions())
        elif args.unsubscribe:
            print(await admin.unsubscribe(args.unsubscribe))
        elif args.base_url:
            if not args.base_url.startswith("https://"):
                print("Авито требует HTTPS для вебхука", file=sys.stderr)
                return 2
            result = await admin.subscribe(args.base_url)
            # Секрет в консоль не печатаем.
            print(f"Подписка зарегистрирована на "
                  f"{admin.webhook_url_for_display(args.base_url)}")
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
