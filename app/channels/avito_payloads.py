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


_ITEM_ID_PATHS: tuple[tuple[str, ...], ...] = (
    ("payload", "value", "item_id"),
    ("payload", "value", "itemId"),
    ("value", "item_id"),
    ("item_id",),
)


def extract_item_id_raw(payload: dict) -> Any:
    """item_id КАК ПРИШЁЛ, без приведения к строке — только для логов.

    `extract_item_id` намеренно возвращает строку (в БД и в фильтре везде
    строки), и из его результата уже не видно, что Авито прислал число.
    А «строка против числа» — первая гипотеза, когда фильтр объявлений не
    сработал, и проверять её по коду вместо лога значит гадать.
    """
    for path in _ITEM_ID_PATHS:
        value = _dig(payload, path)
        if value is not None:
            return value
    return None


def build_event_from_polled_message(
    message: dict,
    *,
    chat_id: str,
    item_id: Optional[str],
    chat_type: Optional[str],
) -> dict:
    """Сообщение из `GET /messenger/v3/.../messages/` → событие вебхука.

    ПОЧЕМУ СБОРКА ЖИВЁТ ЗДЕСЬ, А НЕ В ПОЛЛЕРЕ. Весь этот модуль — знание о
    том, по каким путям в конверте лежат поля. Если собирать событие в
    `app/avito/poller.py`, то же знание окажется в двух файлах, и разъедутся
    они не сразу, а в тот день, когда кто-нибудь поправит один путь в
    экстракторе. Тогда поллер начнёт отдавать событие, из которого конвейер
    молча не прочитает, скажем, chat_type, — и симптомом будет не ошибка, а
    «агент почему-то отвечает не так». Сборщик и разборщики стоят рядом и
    покрыты тестом на эквивалентность (tests/test_poller.py).

    Форма — та же, что у вебхука: payload.value.*. Все экстракторы выше
    обязаны читать отсюда ровно то же, что прочитали бы из настоящего
    вебхука по тому же сообщению.

    item_id ПРИВОДИТСЯ К СТРОКЕ. В API Авито это число, у нас везде строка,
    и сравнение строки с числом молча не совпадает — то есть запрещённое
    объявление тихо становится разрешённым. На этом в проекте уже горели,
    поэтому приведение стоит на границе, а не у вызывающих.
    """
    author_id = None
    for key in ("author_id", "authorId", "user_id"):
        value = message.get(key)
        if isinstance(value, (str, int)) and str(value):
            author_id = str(value)
            break

    created = message.get("created")
    if not isinstance(created, int):
        created = int(created) if isinstance(created, str) and created.isdigit() else None

    value: dict[str, Any] = {
        "id": str(message.get("id")) if message.get("id") is not None else None,
        "chat_id": str(chat_id),
        "author_id": author_id,
        "created": created,
        "type": message.get("type"),
        "content": message.get("content") if isinstance(message.get("content"), dict) else {},
    }
    if chat_type is not None:
        value["chat_type"] = chat_type
    # Ключа item_id у чата без объявления быть НЕ ДОЛЖНО вовсе, а не должен
    # он быть равным None: `_first_scalar` отличает «поля нет» от «поле
    # пустое» только отсутствием, и пустое значение читалось бы как строка.
    if item_id is not None:
        value["item_id"] = str(item_id)

    return {
        "id": value["id"],
        "version": "v3.0.0",
        "timestamp": created,
        "payload": {"type": "message", "value": value},
    }


def extract_created(payload: dict) -> Optional[int]:
    """Unix-секунды создания сообщения — реальное время из Авито, а не
    момент, когда мы его увидели.

    Единственный сигнал, на котором держится `AGENT_MIN_INBOUND_TS`
    (app/pipeline.py): вебхук и поллер несут его под одним и тем же путём
    (см. `build_event_from_polled_message` — форма события намеренно та же,
    что у вебхука), плюс запасной путь по верхнеуровневому `timestamp`
    конверта на случай вебхука без `created` внутри `value`.

    None означает «не смогли определить» — вызывающий код (в отличие от
    большинства экстракторов этого модуля) обязан считать это НЕ «свежим», а
    «неизвестным»: см. докстринг `agent_min_inbound_ts` в app/config.py.
    """
    for path in (
        ("payload", "value", "created"),
        ("value", "created"),
        ("created",),
        ("timestamp",),
    ):
        value = _dig(payload, path)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def extract_chat_type(payload: dict) -> Optional[str]:
    """Тип чата: "u2i" (по объявлению), "u2u"/"a2u" (по профилю).

    ПОДТВЕРЖДЕНО СПЕКОМ (components/schemas/WebhookMessage в официальном
    OpenAPI Авито, см. шапку app/channels/avito_endpoints.py). Там же прямо
    сказано про соседнее поле item_id: «ID объявления, актуально только для
    чатов с типом u2i», nullable. Это и есть ответ на вопрос, почему item_id
    иногда не приходит: у чатов, начатых с профиля продавца, а не с
    объявления, объявления просто нет — и дозапрашивать его через get_chat
    бессмысленно, там будет то же самое.
    """
    return _first_scalar(
        payload,
        [
            ("payload", "value", "chat_type"),
            ("value", "chat_type"),
            ("chat_type",),
        ],
    )


def extract_item_id_from_chat(chat: dict) -> Optional[str]:
    """item_id из ответа GET /messenger/v2/.../chats/{chat_id}.

    ПОДТВЕРЖДЕНО СПЕКОМ (components/schemas/Chat): context.type == "item",
    context.value.id — «ID объявления». Проверка type обязательна: у чата с
    другим типом контекста в value.id лежит идентификатор чего-то другого,
    и принять его за объявление означало бы отвечать по чужому item_id.
    """
    if not isinstance(chat, dict):
        return None
    context = chat.get("context")
    if not isinstance(context, dict) or context.get("type") != "item":
        return None
    value = context.get("value")
    if not isinstance(value, dict):
        return None
    item_id = value.get("id")
    return str(item_id) if isinstance(item_id, (str, int)) and str(item_id) else None


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
