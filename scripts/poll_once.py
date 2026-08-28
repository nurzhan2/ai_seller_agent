"""Пробник поллинга Авито — ЧИТАЕТ И НИЧЕГО НЕ ОТПРАВЛЯЕТ.

    python -m scripts.poll_once --dry
    python -m scripts.poll_once --check-ids
    python -m scripts.poll_once --chat u2i-abc123

Существует раньше самого поллера (app/avito/poller.py) и намеренно: три
вопроса, от ответов на которые зависит его устройство, нельзя решить по
коду — только живым запросом к аккаунту заказчика.

  1. СОВПАДАЮТ ЛИ message_id ВЕБХУКА И v3. Дедуп между каналами построен на
     ключе `avito:seen_message:{message_id}`. Он разводит вебхук и поллер
     только если ОБА канала называют одно и то же сообщение одним и тем же
     идентификатором. В спеке это нигде не обещано: конверт вебхука описан
     слабо (см. докстринг app/channels/avito_payloads.py), а v3 — другой
     эндпоинт. Если идентификаторы разойдутся, дедуп по message_id надо
     менять на составной ключ, и это решение принимается ДО написания
     поллера, а не после. Проверка — режим --check-ids.

  2. СКОЛЬКО ЧАТОВ В АККАУНТЕ и отсортирован ли список по убыванию
     last_message.created. От первого зависит, укладывается ли полная
     пагинация в минутный интервал; от второго — можно ли обрывать проход
     на первой странице без новых сообщений.

  3. ЧТО СЛУЧИТСЯ НА ХОЛОДНОМ СТАРТЕ. Колонка «решение» показывает, кого
     поллер обработал бы, а кого пометил бы seen молча, — до того, как он
     это сделает по-настоящему.

ТЕКСТ СООБЩЕНИЙ НЕ ПЕЧАТАЕТСЯ НИГДЕ, ни в одном режиме — только длина и
структурные поля. Это переписка живых людей, а вывод пробника уходит в
консоль, в историю команд и в чат.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from app.channels.avito import AvitoClient
from app.channels.avito_payloads import extract_item_id_from_chat
from app.channels.outbound_gate import is_listing_allowed
from app.config import get_settings

# Столько последних сообщений тянем из чата в режимах --chat и --check-ids.
# Тот же порядок величины, что будет у POLLER_MESSAGES_PER_CHAT.
MESSAGES_LIMIT = 20
# Сколько чатов проверяем в --check-ids. Больше не нужно: если идентификаторы
# совпадают на десятке чатов, они совпадают в принципе, а если разойдутся —
# это будет видно на первом же.
CHECK_IDS_CHATS = 10
# Попыток на одну страницу списка чатов сверх ретраев самого клиента.
PAGE_RETRIES = 3


# --------------------------------------------------------------------------
# Разбор ответов — терпимо, по образцу app/channels/avito_payloads.py
# --------------------------------------------------------------------------

def _chats_of(page: Any) -> list[dict]:
    """Список чатов из страницы ответа.

    Спек называет поле "chats", но пробник должен показать данные, а не
    упасть на неожиданном имени ключа: пустой список здесь означал бы «в
    аккаунте нет чатов» — ровно тот вывод, ради опровержения которого
    пробник и писался.
    """
    if isinstance(page, list):
        return [c for c in page if isinstance(c, dict)]
    if not isinstance(page, dict):
        return []
    for key in ("chats", "items", "resources", "data"):
        value = page.get(key)
        if isinstance(value, list):
            return [c for c in value if isinstance(c, dict)]
    return []


def _messages_of(page: Any) -> list[dict]:
    if isinstance(page, list):
        return [m for m in page if isinstance(m, dict)]
    if not isinstance(page, dict):
        return []
    for key in ("messages", "items", "resources", "data"):
        value = page.get(key)
        if isinstance(value, list):
            return [m for m in value if isinstance(m, dict)]
    return []


def _last_message(chat: dict) -> dict:
    value = chat.get("last_message")
    return value if isinstance(value, dict) else {}


def _created_of(node: dict) -> Optional[int]:
    """created в API Авито — unix-секунды числом.

    Именно поэтому курсор в chat_cursor будет BigInteger, а не timestamptz:
    сравнение целых не зависит ни от часового пояса, ни от того, как драйвер
    разберёт дату. Здесь принимаем и строку — пробник обязан показать чужой
    формат, а не притвориться, что поля нет.
    """
    for key in ("created", "created_at", "timestamp"):
        value = node.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _chat_title(chat: dict) -> str:
    context = chat.get("context")
    if isinstance(context, dict):
        value = context.get("value")
        if isinstance(value, dict):
            title = value.get("title")
            if isinstance(title, str) and title:
                return title
    return "—"


def _chat_type(chat: dict) -> str:
    """u2i (по объявлению) или u2u/a2u (из профиля).

    В ответе v2 явного поля типа нет — тип выводится из контекста, ровно как
    это делает extract_item_id_from_chat: context.type == "item" означает
    чат по объявлению.
    """
    context = chat.get("context")
    if isinstance(context, dict) and context.get("type") == "item":
        return "u2i"
    return "u2u/a2u"


def _author_of(node: dict) -> Optional[str]:
    for key in ("author_id", "authorId", "user_id"):
        value = node.get(key)
        if isinstance(value, (str, int)) and str(value):
            return str(value)
    return None


def _message_id_of(node: dict) -> Optional[str]:
    value = node.get("id")
    return str(value) if isinstance(value, (str, int)) and str(value) else None


def _age(created: Optional[int], now: int) -> str:
    if created is None:
        return "?"
    delta = now - created
    if delta < 3600:
        return f"{delta // 60}м"
    if delta < 86400:
        return f"{delta // 3600}ч"
    return f"{delta // 86400}д"


# --------------------------------------------------------------------------
# Режим --dry: что сделал бы поллер на холодном старте
# --------------------------------------------------------------------------

async def _fetch_all_chats(
    client: AvitoClient, page_size: int, max_chats: int
) -> tuple[list[dict], int, bool]:
    """Все чаты аккаунта с пагинацией.

    Возвращает (чаты, число запросов, полный ли обход).

    Своя обёртка ретраев поверх той, что уже есть в `AvitoClient._request`,
    и это не дублирование: клиент ретраит ОДИН запрос и, исчерпав попытки,
    бросает наружу — для боевого кода правильно, для пробника нет. Обход
    аккаунта здесь занимает десятки страниц, и терять весь результат из-за
    одной икнувшей (транзиентный DNS на домашнем канале уже случился) —
    значит не получить ответ на вопрос, ради которого пробник и запускают.
    Частичный обход с честной пометкой полезнее, чем traceback.
    """
    chats: list[dict] = []
    offset = 0
    requests = 0
    complete = True

    while len(chats) < max_chats:
        page = None
        for attempt in range(1, PAGE_RETRIES + 1):
            try:
                page = await client.list_chats(limit=page_size, offset=offset)
                break
            except Exception as exc:
                requests += 1
                print(f"  offset={offset}: попытка {attempt}/{PAGE_RETRIES} — "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
                if attempt == PAGE_RETRIES:
                    complete = False
                else:
                    await asyncio.sleep(2 * attempt)

        if page is None:
            print(f"  offset={offset}: страница не получена, обход оборван",
                  file=sys.stderr)
            break

        requests += 1
        batch = _chats_of(page)
        if not batch:
            break
        chats.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size

        # Прогресс в stderr, чтобы длинный обход не выглядел зависанием и
        # чтобы он не смешивался с таблицей в stdout при перенаправлении.
        if requests % 10 == 0:
            print(f"  ...{len(chats)} чатов за {requests} запросов", file=sys.stderr)

    return chats[:max_chats], requests, complete


def _decision(
    chat: dict, our_id: str, now: int, backfill_hours: int, settings: Any
) -> tuple[str, str]:
    """(решение, причина) для чата БЕЗ курсора — то есть на холодном старте.

    Ровно то правило, которое будет в поллере: автоответ только там, где
    последнее сообщение входящее И свежее окна. Всё остальное помечается
    seen без единого исходящего.

    ФИЛЬТР ОБЪЯВЛЕНИЙ УЧИТЫВАЕТСЯ ЗДЕСЬ — через ту же `is_listing_allowed`,
    что стоит на входе конвейера и на границе отправки. Без него колонка
    решения ЗАВЫШАЕТ работу поллера: в первом же прогоне 7 из 13 «свежих»
    чатов оказались по квартире-студии, то есть по объявлению из жёсткого
    запрета, и конвейер отбросил бы их, не заведя даже строки в базе.
    Пробник, который этого не показывает, врёт в самую важную сторону —
    в сторону «агент сейчас всем напишет».
    """
    last = _last_message(chat)
    if not last:
        return "пропуск", "no_messages"

    created = _created_of(last)
    if created is None:
        return "пропуск", "no_created"

    # Фильтр — раньше проверки возраста: запрещённое объявление не станет
    # разрешённым оттого, что сообщение свежее.
    if not is_listing_allowed(extract_item_id_from_chat(chat), settings):
        return "пропуск", "объявление под запретом"

    author = _author_of(last)
    if author is not None and our_id and author == our_id:
        return "пропуск", "outgoing_last"

    if backfill_hours <= 0:
        return "пропуск", "backfill=0"

    if now - created > backfill_hours * 3600:
        return "пропуск", "old"

    return "ОБРАБОТАТЬ", "свежее входящее"


async def run_dry(client: AvitoClient, args: argparse.Namespace) -> int:
    settings = get_settings()
    our_id = settings.avito_user_id
    now = int(datetime.now(timezone.utc).timestamp())

    chats, requests, complete = await _fetch_all_chats(
        client, args.page_size, args.max_chats
    )

    print(f"AVITO_USER_ID: {our_id}")
    print(f"Чатов получено: {len(chats)} за {requests} запрос(ов) "
          f"по {args.page_size} на страницу")
    if not complete:
        print("ВНИМАНИЕ: обход ОБОРВАН на ошибке — числа ниже неполные.")
    elif len(chats) >= args.max_chats:
        print(f"ВНИМАНИЕ: упёрлись в --max-chats={args.max_chats}, "
              "в аккаунте чатов больше.")
    print(f"Окно холодного старта: POLLER_BACKFILL_HOURS={args.backfill_hours}")
    print()

    # Отсортирован ли список по убыванию времени последнего сообщения. От
    # этого зависит, можно ли обрывать проход на первой странице без новых
    # сообщений, — иначе каждый проход тянет весь аккаунт целиком.
    created_seq = [_created_of(_last_message(c)) for c in chats]
    known = [c for c in created_seq if c is not None]
    inversions = [(i, known[i], known[i + 1])
                  for i in range(len(known) - 1) if known[i] < known[i + 1]]
    worst = max((b - a for _, a, b in inversions), default=0)

    print(f"Сортировка по убыванию last_message.created: "
          f"{'СТРОГАЯ' if not inversions else 'НАРУШЕНА'} "
          f"(проверено {len(known)} из {len(chats)})")
    if inversions:
        # Величина нарушения важнее самого факта: сортировка «почти по
        # убыванию» с разбросом в минуты позволяет читать только первые
        # страницы с запасом, а разброс в недели — нет. От этого зависит,
        # можно ли вообще обойтись пагинацией с потолком offset=1000.
        print(f"  нарушений порядка: {len(inversions)}, "
              f"худшее — на {worst} сек ({worst // 86400} сут)")
        print(f"  первые нарушения (позиция, было, стало): {inversions[:5]}")
    if known:
        print(f"  самое свежее: {_age(max(known), now)} назад, "
              f"самое старое: {_age(min(known), now)} назад")
    print()

    header = (f"{'chat_id':<26} {'тип':<8} {'item_id (тип)':<22} "
              f"{'возраст':<8} {'от':<8} {'решение':<11} причина / заголовок")
    print(header)
    print("-" * len(header))

    counts: dict[str, int] = {}
    reasons: dict[str, int] = {}
    zero_items = 0

    for chat in chats:
        chat_id = str(chat.get("id") or chat.get("chat_id") or "?")
        last = _last_message(chat)
        created = _created_of(last)
        author = _author_of(last)

        # item_id печатается С ТИПОМ, как он пришёл из API. В API Авито это
        # ЧИСЛО, у нас везде строка, и «строка против числа» — первая
        # гипотеза, когда фильтр объявлений молча не срабатывает. На этом в
        # проекте уже горели, поэтому тип виден, а не подразумевается.
        raw_item = None
        context = chat.get("context")
        if isinstance(context, dict) and context.get("type") == "item":
            value = context.get("value")
            if isinstance(value, dict):
                raw_item = value.get("id")
        item_cell = f"{raw_item!r}({type(raw_item).__name__})" if raw_item is not None else "—"

        # То же значение через боевую функцию — она обязана вернуть строку.
        normalized = extract_item_id_from_chat(chat)
        if normalized is not None and not isinstance(normalized, str):
            item_cell += " !!НЕ СТРОКА"

        direction = "мы" if (author and our_id and author == our_id) else "клиент"
        decision, reason = _decision(chat, our_id, now, args.backfill_hours, settings)
        counts[decision] = counts.get(decision, 0) + 1
        reasons[reason] = reasons.get(reason, 0) + 1
        if normalized == "0":
            # context.type == "item", но value.id == 0. Для фильтра это
            # ОБЫЧНОЕ объявление со строковым id "0" (не None!), которого
            # нет в чёрном списке, — то есть разрешённое.
            zero_items += 1

        print(f"{chat_id:<26} {_chat_type(chat):<8} {item_cell:<22} "
              f"{_age(created, now):<8} {direction:<8} {decision:<11} "
              f"{reason} / {_chat_title(chat)[:40]}")

    print()
    print(f"ИТОГО на холодном старте: обработать {counts.get('ОБРАБОТАТЬ', 0)}, "
          f"пропустить {counts.get('пропуск', 0)}")
    for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"  {reason}: {count}")
    if zero_items:
        print()
        print(f"ВНИМАНИЕ: чатов с item_id == 0: {zero_items}. "
              "extract_item_id_from_chat отдаёт для них")
        print("строку \"0\", а не None — значит фильтр считает их обычным "
              "разрешённым объявлением.")
    print()
    print("Ни одного исходящего сообщения не отправлено — это пробник.")
    return 0


# --------------------------------------------------------------------------
# Режим --check-ids: главный вопрос, ради которого пробник и написан
# --------------------------------------------------------------------------

async def _webhook_message_ids() -> dict[str, list[str]]:
    """chat_id -> avito_message_id входящих, сохранённых КОНВЕЙЕРОМ.

    Источник — таблица messages: до появления поллера туда писал только
    обработчик вебхука (app/pipeline.py:_handle_message), значит каждый
    avito_message_id там — это идентификатор ИЗ ВЕБХУКА. Именно его и надо
    сравнить с тем, что отдаёт v3.
    """
    from sqlalchemy import select

    from app.db.models import Direction, Message
    from app.db.session import get_sessionmaker

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Message.chat_id, Message.avito_message_id)
                .where(
                    Message.direction == Direction.incoming,
                    Message.avito_message_id.is_not(None),
                )
                .order_by(Message.created_at.desc())
                .limit(500)
            )
        ).all()

    by_chat: dict[str, list[str]] = {}
    for chat_id, message_id in rows:
        by_chat.setdefault(str(chat_id), []).append(str(message_id))
    return by_chat


def _webhook_ids_from_file(path: str) -> dict[str, list[str]]:
    """Те же данные, но из файла — потому что до базы прода не дотянуться.

    У сервиса Postgres на Railway не включён публичный TCP-прокси: DATABASE_URL
    указывает на `postgres.railway.internal`, который резолвится только внутри
    их сети. Снять выгрузку можно `railway ssh` изнутри контейнера, а сверить
    с v3 — только снаружи, где есть доступ в интернет к api.avito.ru. Разрыв
    ровно посередине проверки, поэтому у неё два входа: живая БД и файл.

    Формат — то, что печатает выгрузка: {"rows": [[chat_id, message_id, ...]]}
    либо просто {chat_id: [message_id, ...]}.
    """
    import json

    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)

    if isinstance(data, dict) and "rows" in data:
        by_chat: dict[str, list[str]] = {}
        for row in data["rows"]:
            if isinstance(row, (list, tuple)) and len(row) >= 2:
                by_chat.setdefault(str(row[0]), []).append(str(row[1]))
        return by_chat

    if isinstance(data, dict):
        return {str(k): [str(v) for v in vs] for k, vs in data.items()}

    raise ValueError(f"{path}: неожиданный формат")


async def run_check_ids(client: AvitoClient, args: argparse.Namespace) -> int:
    print("ПРОВЕРКА: совпадают ли message_id вебхука и GET /messenger/v3/.../messages/")
    print()
    print("Если НЕ совпадут — ключ дедупа avito:seen_message:{message_id} не")
    print("разведёт вебхук и поллер, и вместо него нужен составной ключ")
    print("(chat_id + created + хеш текста). Это решается ДО написания поллера.")
    print()

    try:
        if args.ids_file:
            by_chat = _webhook_ids_from_file(args.ids_file)
            print(f"Источник вебхучных id: файл {args.ids_file}")
        else:
            by_chat = await _webhook_message_ids()
            print("Источник вебхучных id: таблица messages")
    except Exception as exc:
        print(f"Источник id недоступен: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(file=sys.stderr)
        print("Нужна база прода — именно там лежат сообщения, пришедшие вебхуком.",
              file=sys.stderr)
        print("У Postgres на Railway публичного адреса нет, поэтому снимите",
              file=sys.stderr)
        print("выгрузку изнутри (railway ssh) и передайте её сюда --ids-file.",
              file=sys.stderr)
        return 1

    if not by_chat:
        print("В таблице messages нет ни одного входящего с avito_message_id.")
        print("Сравнивать не с чем: либо база пустая, либо вебхуки и правда")
        print("не доходили ни разу. Проверьте на базе прода.")
        return 1

    print(f"Чатов с сохранёнными вебхучными id: {len(by_chat)}")
    print()

    matched = mismatched = 0
    for chat_id, webhook_ids in list(by_chat.items())[:CHECK_IDS_CHATS]:
        try:
            page = await client.get_messages(chat_id, limit=MESSAGES_LIMIT)
        except Exception as exc:
            print(f"{chat_id}: v3 не ответил — {type(exc).__name__}: {exc}")
            continue

        v3_ids = {mid for mid in (_message_id_of(m) for m in _messages_of(page)) if mid}
        overlap = set(webhook_ids) & v3_ids

        if overlap:
            matched += 1
            verdict = f"СОВПАЛИ ({len(overlap)} шт.)"
        else:
            mismatched += 1
            verdict = "НЕ СОВПАЛИ"

        print(f"{chat_id}")
        print(f"  из вебхука (в БД): {webhook_ids[:3]}")
        print(f"  из v3:             {sorted(v3_ids)[:3]}")
        print(f"  → {verdict}")
        print()

    print("=" * 70)
    if mismatched == 0 and matched > 0:
        print(f"ВЕРДИКТ: идентификаторы совпадают ({matched} чат(ов) проверено).")
        print("Ключ дедупа avito:seen_message:{message_id} годится как есть,")
        print("правки 1-3 к плану остаются в силе.")
        return 0

    print(f"ВЕРДИКТ: совпало {matched}, разошлось {mismatched}.")
    print("ДЕДУП ПО message_id МЕЖДУ КАНАЛАМИ НЕ РАБОТАЕТ — нужен составной")
    print("ключ. Не писать поллер, пока это не решено.")
    return 2


# --------------------------------------------------------------------------
# Режим --chat: форма сообщений одного чата, без текста
# --------------------------------------------------------------------------

async def run_chat(client: AvitoClient, args: argparse.Namespace) -> int:
    page = await client.get_messages(args.chat, limit=MESSAGES_LIMIT)
    messages = _messages_of(page)

    envelope = sorted(page.keys()) if isinstance(page, dict) else type(page).__name__
    print(f"Чат {args.chat}: сообщений в ответе {len(messages)}")
    print(f"Ключи конверта: {envelope}")
    print()
    print("Текст НЕ печатается — только длина и структурные поля.")
    print()

    for message in messages:
        content = message.get("content")
        text_len = None
        if isinstance(content, dict) and isinstance(content.get("text"), str):
            text_len = len(content["text"])
        print(
            f"id={_message_id_of(message)} "
            f"created={_created_of(message)!r} "
            f"author={_author_of(message)} "
            f"type={message.get('type')!r} "
            f"direction={message.get('direction')!r} "
            f"text_len={text_len} "
            f"ключи={sorted(message.keys())}"
        )
    return 0


# --------------------------------------------------------------------------

async def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    # stderr — тоже: в него уходит ровно то, ради чего пробник запускают,
    # когда всё пошло не так (нет ключей, недоступна база). Прочитать этот
    # текст кракозябрами в консоли Windows значит не прочитать его вовсе.
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dry", action="store_true",
                        help="показать, что поллер сделал бы на холодном старте (по умолчанию)")
    parser.add_argument("--check-ids", action="store_true",
                        help="сравнить message_id вебхука и v3 — главная проверка")
    parser.add_argument("--chat", metavar="CHAT_ID",
                        help="показать форму сообщений одного чата (без текста)")
    parser.add_argument("--ids-file", metavar="PATH",
                        help="взять вебхучные message_id из файла, а не из БД "
                             "(у Postgres на Railway нет публичного адреса)")
    parser.add_argument("--backfill-hours", type=int, default=0,
                        help="окно холодного старта для колонки «решение» (по умолчанию 0)")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-chats", type=int, default=1000)
    args = parser.parse_args()

    settings = get_settings()
    if not settings.avito_user_id or "your_" in settings.avito_user_id:
        print("AVITO_USER_ID не задан (в .env шаблонное значение) — "
              "запросы к Авито невозможны.", file=sys.stderr)
        return 1

    client = AvitoClient(settings=settings)
    try:
        if args.check_ids:
            return await run_check_ids(client, args)
        if args.chat:
            return await run_chat(client, args)
        return await run_dry(client, args)
    finally:
        await client.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
