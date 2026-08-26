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

import re
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


def extract_message_type(payload: dict) -> Optional[str]:
    """Тип содержимого сообщения — "text"/"image"/"link"/"call" и т.п.

    НЕ подтверждено спеком — тот же слабый конверт вебхука, что и у text/
    chat_id выше (см. докстринг модуля), только для типа нет даже примера в
    OpenAPI-описании (там расписаны только методы отправки, не форма
    входящего вебхука). Путь ниже — общеизвестная форма мессенджер-вебхука
    Авито из практики интеграторов, не из официальной документации.
    Используется только для диагностики (app/pipeline.py логирует при
    отсутствии текста) и для best-effort определения "это фото"
    (is_image_message) — ошибка здесь ведёт к молчанию или лишнему «вижу
    фото», а не к неверной цене или брони, поэтому цена ошибки терпимая.
    """
    return _first_scalar(
        payload,
        [
            ("payload", "value", "type"),
            ("value", "type"),
            ("type",),
        ],
    )


_CONTENT_PATHS: tuple[tuple[str, ...], ...] = (
    ("payload", "value", "content"),
    ("value", "content"),
    ("content",),
)


def _extract_content(payload: dict) -> Any:
    for path in _CONTENT_PATHS:
        node = _dig(payload, path)
        if node is not None:
            return node
    return None


def is_image_message(payload: dict) -> bool:
    """Best-effort: сообщение — фото без подписи (см. extract_message_type).

    Основной сигнал — type == "image". Запасной — content содержит ключ
    "image": если тип придёт под другим именем/путём, чем мы предположили,
    это не даст молча принять фото за нераспознанное системное событие.
    """
    if extract_message_type(payload) == "image":
        return True
    content = _extract_content(payload)
    return isinstance(content, dict) and "image" in content


# Поля, в значениях которых может оказаться имя или телефон клиента — даже
# если структура вебхука отличается от ожидаемой (а именно это и предстоит
# выяснить, раз мы вообще логируем сырой payload). Проверяется ПОДСТРОКОЙ, а
# не целым словом: реальные имена полей в этом же проекте — buyer_name,
# author_name (см. app/db/models.py:Chat.buyer_name) — то есть "name" стоит
# после "_", а \b на границе "_" не срабатывает (подчёркивание — символ
# слова в regex), и более строгий вариант с \b пропустил бы именно их.
_PII_KEY_HINT = re.compile(r"(?i)(name|fio|surname|firstname|lastname|patronymic|phone|tel)")
# Похоже на телефон вне зависимости от ключа — на случай, если номер
# окажется там, где мы его не ждали (имя поля не входит в _PII_KEY_HINT).
_PHONE_LIKE = re.compile(r"\+?\d[\d\-\s()]{6,}\d")

# С запасом на реальную глубину картиночного content (payload.value.content.
# image.sizes."WxH" — уже 5 уровней от корня) — цель предохранителя от
# патологически глубоких/зацикленных структур, а не сокрытие полезных
# данных на разумной глубине.
_SANITIZE_MAX_DEPTH = 8
_SANITIZE_MAX_LIST_ITEMS = 5


def _sanitize_for_logging(node: Any, key_hint: Optional[str] = None, depth: int = 0) -> Any:
    if depth > _SANITIZE_MAX_DEPTH:
        return "…"
    if isinstance(node, dict):
        return {
            key: _sanitize_for_logging(value, key_hint=key, depth=depth + 1)
            for key, value in node.items()
        }
    if isinstance(node, list):
        items = [
            _sanitize_for_logging(item, key_hint=key_hint, depth=depth + 1)
            for item in node[:_SANITIZE_MAX_LIST_ITEMS]
        ]
        if len(node) > _SANITIZE_MAX_LIST_ITEMS:
            items.append(f"…ещё {len(node) - _SANITIZE_MAX_LIST_ITEMS}")
        return items
    if isinstance(node, str):
        if key_hint and _PII_KEY_HINT.search(key_hint):
            return "***"
        return _PHONE_LIKE.sub("***", node)
    return node


def describe_payload_for_logging(payload: dict) -> dict:
    """Диагностический снимок вебхука без текста для логов.

    ПОЧЕМУ ЭТО СУЩЕСТВУЕТ. "pipeline: message without text" в логе не
    говорил, ЧТО именно пришло — фото (известный пробел, см. README) или
    что-то со структурой, отличной от ожидаемой (системное событие, другой
    тип). Без сырых данных вслепую чинить нечего.

    Маскирует значения под «подозрительными» ключами (name/phone/...) и
    ЛЮБУЮ подстроку, похожую на телефон, независимо от ключа: реальная
    форма вебхука для типов, отличных от text, не подтверждена
    документацией, поэтому маскировка только по имени поля не была бы
    гарантией — телефон мог бы оказаться под именем, которого мы не
    предусмотрели.
    """
    return {
        "top_level_keys": sorted(payload.keys()) if isinstance(payload, dict) else None,
        "message_type": extract_message_type(payload),
        "sanitized": _sanitize_for_logging(payload),
    }
