"""Поллинг Авито — ОСНОВНОЙ канал получения сообщений.

ПОЧЕМУ ОН ВООБЩЕ ЕСТЬ. Вебхуки по объявлениям комплекса не доставляются.
Это не догадка: подписка активна, ручной POST на вебхук прогоняет конвейер
целиком, а в базе за всю историю 49 входящих, и ВСЕ u2i-чаты среди них — по
объявлениям из чёрного списка (квартира-студия, продажа комплекса). По
разрешённым объявлениям бань и гриль-домиков — ни одного события. Канал не
мёртв, он молчит избирательно, и ждать от поддержки объяснений дороже, чем
опрашивать.

КАК УСТРОЕН ПРОХОД:
    1. захват в Redis — чтобы две реплики не опрашивали аккаунт разом;
    2. список чатов постранично (потолок offset=1000, см. ниже);
    3. курсоры всей пачки одним запросом к БД;
    4. для каждого чата с новыми сообщениями — забрать их и подать в
       СУЩЕСТВУЮЩИЙ конвейер входящих тем же событием, что шлёт вебхук;
    5. курсор двигается ПО КАЖДОМУ обработанному сообщению.

ЧЕТЫРЕ ВЕЩИ, В КОТОРЫХ ЛЕГКО ОШИБИТЬСЯ, И ПОТОМУ ОНИ ЗДЕСЬ ЯВНО:

  * OFFSET УПИРАЕТСЯ В 1000. `GET /messenger/v2/.../chats?offset=1100`
    отвечает 400 — проверено живьём. Пагинацией нельзя обойти аккаунт
    целиком. Зато потолок ограничивает и работу: полный проход стоит ровно
    11 запросов. Чаты за границей невидимы; список сортируется по убыванию
    времени последнего сообщения (8 нарушений порядка на 1098 чатов,
    худшее — 4 суток), поэтому новая активность всплывает наверх сама.

  * КУРСОР ДВИГАЕТСЯ ПОСООБЩЕНИЙНО, а не по максимуму батча. Иначе одно
    упавшее сообщение из десяти уносит курсор за все десять, и девять
    исчезают молча. Упало — этот чат бросаем, остальные обрабатываем.

  * СРАВНЕНИЕ `>=`, А НЕ `>`. Время секундное, два сообщения в одну
    секунду — обычное дело. От повторной подачи защищает не строгое
    неравенство, а список виденных id внутри той же секунды
    (`CursorRecord.seen_ids`) плюс дедуп по message_id.

  * ГУАРД «СВОЁ ЛИ ЭТО ОБЪЯВЛЕНИЕ». Вебхук приходил только по чатам,
    которые Авито считал нашими. Поллер же видит ВЕСЬ ящик, включая чаты,
    где владелец аккаунта сам покупатель: «Репетитор по физике ЕГЭ»,
    «Покос травы триммером», «Подставка для чайника Tefal». Их 33 на 1100,
    и в чёрном списке их нет — то есть без гуарда агент ответил бы
    репетитору прайсом на баню. Плюс 109 чатов с `item_id == 0`, для
    которых `extract_item_id_from_chat` отдаёт строку "0", и фильтр считает
    их обычным разрешённым объявлением. Оба класса закрывает одна проверка:
    подаём в конвейер только чаты, чей item_id есть среди объявлений
    аккаунта. Это временная мера до таблицы `item_scope` (блок 1), и она
    живёт ЗДЕСЬ, в поллере, а не в общем фильтре — чтобы не менять поведение
    боевого пути вебхука ради чужой проблемы.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from app.avito.cursors import CursorRecord, CursorStore
from app.channels.avito_payloads import (
    build_event_from_polled_message,
    extract_item_id_from_chat,
)

logger = logging.getLogger("parmangal.poller")

LOCK_KEY = "avito:poller:lock"


@dataclass
class PassStats:
    """Итог одного прохода — одной строкой в лог.

    Считается всё, что потом спросят при разборе «почему агент промолчал»:
    сколько чатов увидели, у скольких был новый хвост, сколько сообщений
    ушло в конвейер, сколько чатов пропущено и по какой причине.
    """

    chats_seen: int = 0
    chats_with_new: int = 0
    messages_fed: int = 0
    cold_start_skipped: int = 0
    failed_chats: int = 0
    requests: int = 0
    truncated_chats: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


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


def _chats_of(page: Any) -> list[dict]:
    if isinstance(page, list):
        return [c for c in page if isinstance(c, dict)]
    if not isinstance(page, dict):
        return []
    for key in ("chats", "items", "resources", "data"):
        value = page.get(key)
        if isinstance(value, list):
            return [c for c in value if isinstance(c, dict)]
    return []


def _created_of(node: dict) -> Optional[int]:
    for key in ("created", "created_at", "timestamp"):
        value = node.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _author_of(node: dict) -> Optional[str]:
    for key in ("author_id", "authorId", "user_id"):
        value = node.get(key)
        if isinstance(value, (str, int)) and str(value):
            return str(value)
    return None


def _message_id_of(node: dict) -> Optional[str]:
    value = node.get("id")
    return str(value) if isinstance(value, (str, int)) and str(value) else None


def _chat_id_of(chat: dict) -> Optional[str]:
    for key in ("id", "chat_id"):
        value = chat.get(key)
        if isinstance(value, (str, int)) and str(value):
            return str(value)
    return None


def _chat_type_of(chat: dict) -> str:
    """u2i, если у чата контекст объявления; иначе обращение из профиля.

    Тот же признак, по которому `extract_item_id_from_chat` решает, есть ли
    у чата объявление, — context.type == "item". Отдельного поля с типом в
    ответе v2 нет.
    """
    context = chat.get("context")
    if isinstance(context, dict) and context.get("type") == "item":
        return "u2i"
    return "u2u"


class AvitoPoller:
    def __init__(
        self,
        *,
        client: Any,
        pipeline: Any,
        cursors: CursorStore,
        settings: Any,
        redis: Any = None,
        items_provider: Any = None,
        now_fn=lambda: datetime.now(timezone.utc),
    ):
        self.client = client
        self.pipeline = pipeline
        self.cursors = cursors
        self.settings = settings
        self.redis = redis
        # Callable[[], Awaitable[set[str]]] — идентификаторы объявлений
        # аккаунта. Отдельным объектом, а не «сходить в AvitoItemsClient
        # прямо здесь»: у него свой ритм обновления (раз в час) и свой лимит
        # (25 запросов в минуту), и тесту незачем это воспроизводить.
        self.items_provider = items_provider
        self.now_fn = now_fn
        self._lock_token: Optional[str] = None

    # -- захват ------------------------------------------------------------

    async def _acquire_lock(self) -> bool:
        """Один опрашивающий на аккаунт.

        Без Redis захват не берётся вовсе и проход идёт как есть — на одном
        процессе это верно, а поднимать Redis ради теста незачем.
        """
        if self.redis is None:
            return True
        token = uuid.uuid4().hex
        ttl = max(self.settings.poller_interval_seconds * 2, 120)
        was_set = await self.redis.set(LOCK_KEY, token, nx=True, ex=ttl)
        if not was_set:
            return False
        self._lock_token = token
        return True

    async def _still_own_lock(self) -> bool:
        """Владеем ли мы захватом ПРЯМО СЕЙЧАС.

        Проход по большому аккаунту длится дольше, чем кажется, и захват
        может протухнуть на середине — тогда вторая реплика начнёт свой
        проход, а мы продолжим писать курсоры от её имени. Поэтому владение
        проверяется перед каждой записью курсора, а не один раз на входе.
        """
        if self.redis is None or self._lock_token is None:
            return True
        current = await self.redis.get(LOCK_KEY)
        if isinstance(current, bytes):
            current = current.decode()
        return current == self._lock_token

    async def _renew_lock(self) -> None:
        if self.redis is None or self._lock_token is None:
            return
        if await self._still_own_lock():
            ttl = max(self.settings.poller_interval_seconds * 2, 120)
            await self.redis.expire(LOCK_KEY, ttl)

    async def _release_lock(self) -> None:
        if self.redis is None or self._lock_token is None:
            return
        try:
            if await self._still_own_lock():
                await self.redis.delete(LOCK_KEY)
        except Exception:
            logger.warning("poller: не удалось снять захват, истечёт сам")
        finally:
            self._lock_token = None

    # -- проход ------------------------------------------------------------

    async def run_pass(self) -> PassStats:
        stats = PassStats()

        if not await self._acquire_lock():
            logger.info("poller: проход пропущен — опрашивает другая реплика")
            return stats

        try:
            own_items = await self._own_item_ids()
            if own_items is None:
                # Гуард без данных — это не «пропускать всех», а «не трогать
                # никого»: тот же выбор, что у OutboundGate.is_allowed, где
                # неудача определения item_id означает запрет. Молчаливый
                # проход лучше, чем прайс на баню в ответ репетитору.
                logger.error(
                    "poller: список объявлений аккаунта недоступен и снимка нет — "
                    "проход отменён, ни одно сообщение не подано"
                )
                return stats

            chats = await self._fetch_chats(stats)
            cursor_by_chat = await self.cursors.load(
                [c for c in (_chat_id_of(ch) for ch in chats) if c]
            )

            for index, chat in enumerate(chats):
                if index % 50 == 0:
                    await self._renew_lock()
                try:
                    await self._handle_chat(chat, cursor_by_chat, own_items, stats)
                except Exception:
                    stats.failed_chats += 1
                    logger.exception(
                        "poller: чат не обработан, курсор оставлен на месте",
                        extra={"chat_id": _chat_id_of(chat)},
                    )
        finally:
            await self._release_lock()

        logger.info(
            "poller: проход завершён — чатов %d, с новыми %d, подано сообщений %d, "
            "пропущено холодным стартом %d, сбоев %d, запросов %d, обрезано хвостов %d, "
            "причины пропуска %s",
            stats.chats_seen, stats.chats_with_new, stats.messages_fed,
            stats.cold_start_skipped, stats.failed_chats, stats.requests,
            stats.truncated_chats, stats.reasons,
        )
        return stats

    # -- список объявлений аккаунта ---------------------------------------

    async def _own_item_ids(self) -> Optional[set[str]]:
        if self.items_provider is None:
            # Гуард выключен намеренно (тесты, локальный прогон) — тогда
            # решает только общий фильтр объявлений внутри конвейера.
            return set()
        try:
            return await self.items_provider()
        except Exception:
            logger.exception("poller: не удалось получить объявления аккаунта")
            return None

    # -- список чатов ------------------------------------------------------

    async def _fetch_chats(self, stats: PassStats) -> list[dict]:
        chats: list[dict] = []
        offset = 0
        page_size = self.settings.poller_chats_page_size

        while offset <= self.settings.poller_max_offset:
            try:
                page = await self.client.list_chats(limit=page_size, offset=offset)
            except Exception:
                # Один сбой страницы не должен отменять уже собранное: чаты
                # с начала списка — самые свежие, и обработать их лучше, чем
                # не обработать ничего. Следующий проход через минуту.
                logger.exception(
                    "poller: страница чатов не получена, обход оборван",
                    extra={"offset": offset},
                )
                break
            stats.requests += 1

            batch = _chats_of(page)
            if not batch:
                break
            chats.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size

        stats.chats_seen = len(chats)
        return chats

    # -- один чат ----------------------------------------------------------

    async def _handle_chat(
        self,
        chat: dict,
        cursor_by_chat: dict[str, CursorRecord],
        own_items: set[str],
        stats: PassStats,
    ) -> None:
        chat_id = _chat_id_of(chat)
        if not chat_id:
            stats.skip("no_chat_id")
            return

        item_id = extract_item_id_from_chat(chat)
        last = chat.get("last_message")
        last = last if isinstance(last, dict) else {}
        last_created = _created_of(last)

        cursor = cursor_by_chat.get(chat_id)

        # Гуард «своё ли объявление» — до всего остального, включая холодный
        # старт: чужой чат не должен даже попадать в список «пропущенных с
        # кнопкой обработать», иначе оператор однажды нажмёт её у репетитора.
        if own_items and (item_id is None or item_id not in own_items):
            if cursor is None or not cursor.cold_start_skipped:
                await self._save_cursor(
                    CursorRecord(chat_id, last_created or 0, (), True,
                                 "not_our_listing", item_id)
                )
            stats.skip("not_our_listing")
            return

        if last_created is None:
            if cursor is None:
                await self._save_cursor(
                    CursorRecord(chat_id, 0, (), True, "no_messages", item_id)
                )
                stats.cold_start_skipped += 1
            stats.skip("no_messages")
            return

        if cursor is None:
            decision, reason = self._cold_start_decision(last, last_created)
            if decision == "skip":
                await self._save_cursor(
                    CursorRecord(chat_id, last_created, (), True, reason, item_id)
                )
                stats.cold_start_skipped += 1
                stats.skip(reason)
                return
            # Обрабатываем: курсор начинается ДО последнего сообщения, чтобы
            # оно само попало в выборку.
            cursor = CursorRecord(chat_id, 0, (), False, None, item_id)

        if cursor.already_seen(_message_id_of(last), last_created):
            stats.skip("up_to_date")
            return

        stats.chats_with_new += 1
        await self._drain_chat(chat_id, item_id, _chat_type_of(chat), cursor, stats)

    def _cold_start_decision(self, last: dict, last_created: int) -> tuple[str, str]:
        """Что делать с чатом, у которого курсора ещё нет.

        Автоответ только там, где последнее сообщение ВХОДЯЩЕЕ и СВЕЖЕЕ
        окна. Всё остальное помечается прочитанным без единого исходящего:
        реанимация старой переписки — решение человека, а не побочный эффект
        первого запуска.
        """
        author = _author_of(last)
        our_id = getattr(self.settings, "avito_user_id", "") or ""
        if author is not None and our_id and author == our_id:
            return "skip", "outgoing_last"

        hours = self.settings.poller_backfill_hours
        if hours <= 0:
            return "skip", "backfill_disabled"

        now = int(self.now_fn().timestamp())
        if now - last_created > hours * 3600:
            return "skip", "old"
        return "process", "fresh_incoming"

    async def _drain_chat(
        self,
        chat_id: str,
        item_id: Optional[str],
        chat_type: str,
        cursor: CursorRecord,
        stats: PassStats,
    ) -> None:
        new_messages = await self._fetch_new_messages(chat_id, cursor, stats)
        if not new_messages:
            return

        for message in new_messages:
            created = _created_of(message)
            message_id = _message_id_of(message)

            event = build_event_from_polled_message(
                message, chat_id=chat_id, item_id=item_id,
                chat_type=chat_type if item_id is not None else "u2u",
            )
            handled = await self.pipeline.handle_message(event, source="poller")
            if not handled:
                # Конвейер не справился. Курсор НЕ двигаем: сообщение должно
                # вернуться следующим проходом. Заявку дедупа конвейер уже
                # снял сам (app/channels/inbound_dedup.py), иначе повтор
                # молча отбросился бы как дубль.
                logger.warning(
                    "poller: сообщение не обработано, чат брошен до следующего прохода",
                    extra={"chat_id": chat_id, "message_id": message_id},
                )
                return

            stats.messages_fed += 1
            cursor = cursor.advanced_by(message_id, created)
            if not await self._save_cursor(cursor):
                return

    async def _fetch_new_messages(
        self, chat_id: str, cursor: CursorRecord, stats: PassStats
    ) -> list[dict]:
        """Сообщения новее курсора, по возрастанию времени.

        Страницами до курсора, а не одной выборкой: если за время простоя
        накопилось больше `poller_messages_page_size`, обрывать хвост нельзя —
        это ровно сценарий «вернулись после суток даунтайма». Упёрлись в
        потолок страниц — в лог уходит warning, а не тишина.
        """
        page_size = self.settings.poller_messages_page_size
        collected: list[dict] = []
        reached_cursor = False

        for page_index in range(self.settings.poller_max_message_pages):
            page = await self.client.get_messages(
                chat_id, limit=page_size, offset=page_index * page_size
            )
            stats.requests += 1
            batch = _messages_of(page)
            if not batch:
                reached_cursor = True
                break

            for message in batch:
                created = _created_of(message)
                if cursor.already_seen(_message_id_of(message), created):
                    reached_cursor = True
                    continue
                collected.append(message)

            if reached_cursor or len(batch) < page_size:
                reached_cursor = True
                break

        if not reached_cursor:
            stats.truncated_chats += 1
            logger.warning(
                "poller: упёрлись в потолок страниц (%d x %d), часть истории чата "
                "не прочитана — поднимите POLLER_MAX_MESSAGE_PAGES",
                self.settings.poller_max_message_pages, page_size,
                extra={"chat_id": chat_id},
            )

        # v3 отдаёт от свежих к старым; конвейеру нужно в порядке разговора.
        collected.sort(key=lambda m: (_created_of(m) or 0, str(_message_id_of(m))))
        return collected

    async def _save_cursor(self, cursor: CursorRecord) -> bool:
        """False — захват потерян, писать нельзя и проход надо свернуть."""
        if not await self._still_own_lock():
            logger.error(
                "poller: захват потерян на середине прохода — курсор не записан, "
                "проход свёрнут",
                extra={"chat_id": cursor.chat_id},
            )
            return False
        await self.cursors.save(cursor)
        return True


async def supervised_poller(poller: AvitoPoller, *, interval_seconds: int) -> None:
    """Периодический проход поллера.

    Тот же приём изоляции сбоя, что у `supervised_touch_scheduler`: один
    плохой проход (Авито или БД недоступны) не должен останавливать все
    следующие. Пауза ПОСЛЕ прохода, а не до, — два прохода никогда не идут
    внахлёст, даже если один затянулся.
    """
    while True:
        try:
            await poller.run_pass()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("poller: проход упал целиком")
        await asyncio.sleep(interval_seconds)
