"""Разбор тел вебхуков Авито.

Отдельно от `avito_endpoints.py` намеренно: там — спецификация (пути, лимиты,
имена полей), здесь — терпимый разбор входящих payload'ов. Конверт вебхука
в OpenAPI-спеке описан слабее, чем сами эндпоинты, поэтому читаем поля по
нескольким вероятным путям, а не по одному жёсткому.

Возврат None означает «не смогли определить» — вызывающий код обязан
деградировать (например, положиться на уникальный индекс в БД), а не
подставлять догадку.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional


def _dig(payload: dict, path: Iterable[str]) -> Any:
    node: Any = payload
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _first_scalar(payload: dict, paths: Iterable[Iterable[str]]) -> Optional[str]:
    for path in paths:
        value = _dig(payload, path)
        if isinstance(value, (str, int)) and str(value):
            return str(value)
    return None


def extract_message_id(payload: dict) -> Optional[str]:
    """Идентификатор сообщения — ключ идемпотентности вебхука."""
    return _first_scalar(
        payload,
        [
            ("payload", "value", "id"),
            ("payload", "id"),
            ("value", "id"),
            ("id",),
        ],
    )


def extract_chat_id(payload: dict) -> Optional[str]:
    return _first_scalar(
        payload,
        [
            ("payload", "value", "chat_id"),
            ("payload", "value", "chatId"),
            ("value", "chat_id"),
            ("chat_id",),
        ],
    )


def extract_author_id(payload: dict) -> Optional[str]:
    return _first_scalar(
        payload,
        [
            ("payload", "value", "author_id"),
            ("payload", "value", "authorId"),
            ("value", "author_id"),
            ("author_id",),
        ],
    )


def extract_item_id(payload: dict) -> Optional[str]:
    return _first_scalar(
        payload,
        [
            ("payload", "value", "item_id"),
            ("payload", "value", "itemId"),
            ("value", "item_id"),
            ("item_id",),
        ],
    )


def extract_text(payload: dict) -> Optional[str]:
    value = _first_scalar(
        payload,
        [
            ("payload", "value", "content", "text"),
            ("payload", "value", "text"),
            ("value", "content", "text"),
            ("text",),
        ],
    )
    return value


def is_outgoing_echo(payload: dict, our_user_id: str) -> bool:
    """True, если это эхо нашего же сообщения.

    Авито присылает вебхук и на исходящие сообщения. Без этой проверки агент
    отвечал бы сам себе в бесконечном цикле.
    """
    author = extract_author_id(payload)
    return bool(author) and bool(our_user_id) and str(author) == str(our_user_id)
