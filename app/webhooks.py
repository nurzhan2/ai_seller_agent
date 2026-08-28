"""Приём вебхуков Авито.

ЗАЩИТА — СЕКРЕТ В ПУТИ, А НЕ ПОДПИСЬ. Это осознанное решение, не недоделка.
Авито присылает заголовок `x-avito-messenger-signature`, но алгоритм подписи
не опубликован. Правдоподобно угаданная проверка подписи опаснее её
отсутствия: неверный путь падает громко, а неверная проверка подписи МОЛЧА
ПРИНИМАЕТ подделки. Поэтому вебхук регистрируется по адресу
`/webhook/avito/{AVITO_WEBHOOK_SECRET}`, секрет сравнивается через
`secrets.compare_digest`, а любой другой путь отдаёт 404.

Не «чините» это, добавив угаданный HMAC. Если Авито опубликует алгоритм —
добавьте проверку подписи ВТОРЫМ слоем, не убирая секретный путь.
Подробности — в app/channels/avito_endpoints.py.

Две другие важные характеристики:
  * отвечаем мгновенно — таймаут колбэка у Авито порядка двух секунд, поэтому
    подтверждаем сразу, а работу делаем в фоне;
  * обрабатываем каждое сообщение один раз — ретраи и at-least-once доставка
    нормальны, а дубль входящего означает, что агент дважды ответит на один
    вопрос.

ДЕДУПЛИКАЦИЯ ЗДЕСЬ БОЛЬШЕ НЕ ЖИВЁТ, и это не упрощение, а исправление. Она
переехала в `app/channels/inbound_dedup.py` и вызывается конвейером, потому
что каналов приёма стало два: поллер (`app/avito/poller.py`) зовёт конвейер
напрямую и мимо этого модуля. Проверка, оставшаяся здесь, разводила бы только
вебхук сам с собой, а вебхук с поллером — нет, и агент отвечал бы на одно
сообщение дважды. Не возвращайте её сюда: тогда сообщение будет заявлено
дважды — здесь и в конвейере, — и конвейер отбросит собственное входящее как
дубль самого себя.
"""

from __future__ import annotations

import logging
import secrets as secrets_mod
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, BackgroundTasks, Request, Response, status

from app.channels.avito_payloads import extract_message_id
from app.config import get_settings

logger = logging.getLogger("parmangal.webhook")

router = APIRouter()

WEBHOOK_PATH_TEMPLATE = "/webhook/avito/{secret}"

# Проставляется фабрикой приложения; инъекция нужна, чтобы тестам не требовалось
# поднимать конвейер целиком.
_handler: Optional[Callable[[dict], Awaitable[None]]] = None


def configure(handler: Optional[Callable[[dict], Awaitable[None]]] = None) -> None:
    """Redis сюда больше не передаётся: дедупликация переехала в конвейер
    (см. шапку модуля и app/channels/inbound_dedup.py)."""
    global _handler
    _handler = handler


def webhook_path(secret: str) -> str:
    return WEBHOOK_PATH_TEMPLATE.format(secret=secret)


def secret_matches(candidate: str) -> bool:
    """Сравнение постоянного времени — обычное `==` даёт таймингову утечку,
    по которой секрет можно подобрать посимвольно."""
    expected = get_settings().avito_webhook_secret.get_secret_value()
    if not expected:
        return False
    return secrets_mod.compare_digest(candidate, expected)


@router.post("/webhook/avito/{secret}", status_code=status.HTTP_200_OK)
async def avito_webhook(
    secret: str,
    request: Request,
    background: BackgroundTasks,
) -> Response:
    if not secret_matches(secret):
        # Именно 404, а не 403: сканирующему не сообщаем, что такой маршрут
        # вообще существует.
        logger.warning("webhook rejected: bad secret in path")
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    try:
        payload = await request.json()
    except Exception:
        logger.warning("webhook body is not json")
        return Response(status_code=status.HTTP_200_OK)

    message_id = extract_message_id(payload)

    if _handler is not None:
        background.add_task(_handler, payload)
    else:
        # Обработчик не подключён (configure() не звали или звали без
        # handler) — сообщение исчезает бесследно, а Авито получает 200 и
        # считает доставку успешной. Снаружи это выглядит ровно как
        # «вебхуки не приходят».
        logger.error(
            "webhook: обработчик не подключён, сообщение отброшено",
            extra={"message_id": message_id},
        )

    # Всегда 200 и всегда сразу. Медленный ответ означает ретрай со стороны
    # Авито и повторную обработку того же сообщения.
    return Response(status_code=status.HTTP_200_OK)
