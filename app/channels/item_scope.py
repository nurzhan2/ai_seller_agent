"""item_scope — allow/deny по item_id, без ручного ведения списка объявлений.

ЗАЧЕМ. До этого модуля область действия агента задавалась `AVITO_BLOCKED_ITEMS`
(app/config.py) — id, зашитыми в код. Работало, но каждое НОВОЕ
объявление комплекса (баня, гриль-домик, купол) требовало ручной правки
переменной окружения на деплое. Здесь вместо статического списка —
классификация по заголовку: фоновая задача раз в час тянет все объявления
аккаунта (`scripts/export_listings.py`/`AvitoItemsClient`) и раскладывает их
по `classify_title`, а `ItemScopeResolver` — единая точка, которую вызывает
`app/channels/outbound_gate.py:is_listing_allowed` (единственный фильтр
исходящих, см. его докстринг) — решает по этой таблице, а для ВООБЩЕ новых
item_id, которых таблица ещё не видела, догружает карточку синхронно.

ПОРЯДОК ПРОВЕРОК В `ItemScopeResolver.resolve` — ЖЁСТКИЙ DENY ПЕРВЫМ:

  1. `item_id == "0"` — контекст объявления в чате есть, но Авито прислал
     `value.id == 0`. Не объявление вовсе (109 чатов на 1100 живых — см.
     app/avito/own_items.py). Решается без единого запроса.
  2. Зашитые id (`settings.avito_blocked_items`, DEFAULT_BLOCKED_ITEMS в
     app/config.py) — DENY ВСЕГДА, даже если заголовок матчит allow-слова.
     Проверено живьём: ДВА разных объявления о продаже ВСЕГО КОМПЛЕКСА —
     «Банный комплекс габ. Торга нет, но смотрите» (7980739861) и «Банный
     комплекс "Чайка" инвестиционная возможность» (7948732527) — оба
     содержат слово «банный» из allow-списка ниже; это продажа бизнеса
     целиком, а не бронирование конкретной зоны. Второе объявление сейчас
     ловится ещё и по слову «инвестиционная возможность» (deny-список
     ниже), но хардкод по id остаётся: он не сломается на следующей
     переформулировке того же объявления, которая уберёт это конкретное
     слово, — ровно то, что уже случилось с первым (заголовок без единого
     deny-слова). Проверяется ПЕРЕД таблицей `item_scope`, а не через
     закешированную строку в ней: список читается из настроек заново на
     каждый вызов, чтобы правка `AVITO_BLOCKED_ITEMS` подействовала
     сразу, а не после следующего часового прохода классификатора.
  3. Объявление не наше — владелец аккаунта сам покупатель (33 чата на
     1100: «Репетитор по физике ЕГЭ», «Покос травы триммером», «Подставка
     для чайника Tefal»). Ни один из этих заголовков не матчит deny-слова
     выше — без отдельной проверки такой чат получил бы обычную
     классификацию (allow, раз нет запрещённых слов) и агент ответил бы
     репетитору прайсом на баню.
  4. Таблица `item_scope` — уже классифицированные объявления, самый
     частый путь после первого часа работы.
  5. Неизвестный item_id (не встречался ранее, ЕЩЁ НЕ НАШ по снимку выше) —
     синхронная догрузка карточки объявления (`AvitoItemsClient`) и
     классификация по заголовку. Результат сохраняется в `item_scope`, чтобы
     повторный вызов по тому же item_id больше не стоил обращения к Авито.
     Сбой самого запроса (не «объявления нет», а именно сетевой/API сбой)
     — это НЕ повод молчать живому клиенту: `classify_title` откатывается
     на allow, если уже известный (переданный отдельно) заголовок не матчит
     deny-слова — тот же принцип, что у `AVITO_BLOCKED_ITEMS`: список deny
     — основной инструмент фильтрации, отсутствие сигнала — это разрешение.
     Результат сбоя НЕ кешируется — при следующем обращении догрузка
     повторится.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Protocol

logger = logging.getLogger("parmangal.item_scope")

ALLOW = "allow"
DENY = "deny"

# Авито присылает context.type == "item" с value.id == 0 у части чатов без
# реального объявления. extract_item_id_from_chat отдаёт для них строку "0"
# (не None), и без этой проверки общий фильтр видел бы обычное разрешённое
# объявление — см. app/avito/own_items.py.
ZERO_ITEM_ID = "0"

# Слова комплекса про баню и смежные зоны. Проверка ПОДСТРОКОЙ без \b: в
# заголовках Авито попадаются словоформы («банная зона», «баня-бочка»), и
# более строгий вариант с границей слова часть из них пропустил бы.
_ALLOW_WORDS = (
    "баня", "банный", "парная", "сауна", "чан", "купель",
    "купол", "шатёр", "юрта", "гриль", "беседка",
)
# Посторонние для комплекса объявления заказчика — расширено живым прогоном
# scripts/sync_item_scope.py 2026-08-28 против прода (55 объявлений
# аккаунта, 1100 чатов): часть посторонних объявлений формулирует то же
# самое другими словами и словоформами, чем исходный список ниже, и
# проскакивала в allow через no_keyword_match.
#
#   * "апартаменты"           — квартиры, у которых в заголовке не слово
#                                "квартира" (пример: 8012379561).
#   * "глемпинг" РЯДОМ с       — вторая написанная через "е" форма
#     "глэмпинг" (через "э")     "глэмпинг" (пример: 7947933227, 7948766358)
#                                — обе формы живут в реальных заголовках.
#   * "горничная",             — вакансии, в заголовке которых нет слова
#     "уборщица",                "вакансия" (примеры: "Горничная в
#     "рабочий по обслуживанию",  загородный комплекс", "Рабочий по
#     "персонал", "требуется"     обслуживанию зданий и территорий").
#   * "инвестиционная          — объявления о продаже бизнеса/комплекса
#     возможность",              под инвестиционным углом, а не о
#     "инвестиции",               бронировании зоны.
#     "продажа бизнеса",
#     "готовый бизнес"
#   * "арендный бизнес"        — тот же случай, что и "аренда бизнеса"
#                                ниже, другим порядком слов (пример:
#                                8076019723 "Готовый арендный бизнес").
#
# "требуется" и "персонал" — широкие слова: риск ложного deny на
# объявлении комплекса, где они встретятся не в значении вакансии, признан
# и принят сознательно (короткие заголовки объявлений это маловероятно).
_DENY_WORDS = (
    "вакансия", "продам", "квартира", "студия",
    "апартаменты",
    "глэмпинг", "глемпинг",
    # Оба порядка слов — реальные заголовки используют и тот, и другой
    # (см. живую находку выше: 8076019723 "арендный бизнес" не матчился
    # единственной прежней формулировкой "аренда бизнеса").
    "аренда бизнеса", "бизнес в аренду", "арендный бизнес",
    "готовый бизнес", "продажа бизнеса",
    "горничная", "уборщица", "рабочий по обслуживанию", "персонал", "требуется",
    "инвестиционная возможность", "инвестиции",
)

ALLOW_TITLE_RE = re.compile("|".join(_ALLOW_WORDS), re.IGNORECASE)
DENY_TITLE_RE = re.compile("|".join(_DENY_WORDS), re.IGNORECASE)


def classify_title(title: Optional[str]) -> tuple[str, str]:
    """(decision, reason) по заголовку объявления — без учёта жёсткого deny.

    DENY ПРОВЕРЯЕТСЯ ПЕРВЫМ И ПОБЕЖДАЕТ: заголовок, где встречаются и
    deny-, и allow-слово одновременно («Банный комплекс "Чайка"
    инвестиционная возможность» матчит и «банный», и «инвестиционная
    возможность»), классифицируется как DENY. Само по себе это НЕ
    гарантия: заголовок «Банный комплекс габ. Торга нет, но смотрите»
    (7980739861, реальный, см. app/config.py:DEFAULT_BLOCKED_ITEMS) матчит
    ТОЛЬКО allow-слово «банный» и без единого deny-слова получил бы allow —
    ровно тот случай, на который существует жёсткий deny по id (см.
    докстринг модуля): продажа бизнеса целиком не обязана называться
    словами из deny-списка, а вот про баню обычно упоминает.

    Отсутствие заголовка ИЛИ заголовок, не матчащий ни один список, —
    ALLOW. Тот же принцип, что у `AVITO_BLOCKED_ITEMS` (app/config.py):
    список deny — основной инструмент фильтрации, отсутствие сигнала —
    разрешение, а не запрет. Так новое объявление комплекса, чей заголовок
    ещё не придумали составители списка allow-слов, не блокируется молча.
    """
    if not title:
        return ALLOW, "no_title"
    if DENY_TITLE_RE.search(title):
        return DENY, "title_matches_deny"
    if ALLOW_TITLE_RE.search(title):
        return ALLOW, "title_matches_allow"
    return ALLOW, "no_keyword_match"


def hard_deny_ids_from_settings(settings: Any) -> frozenset[str]:
    """Пять (или сколько выставлено) зашитых id — строками, как и везде
    в проекте (см. комментарий у `is_listing_allowed` в outbound_gate.py:
    сравнение строки с числом молча не совпадает)."""
    return frozenset(str(i) for i in (getattr(settings, "avito_blocked_items", None) or []))


def classify_listing(item_id: str, title: Optional[str], hard_deny_ids: frozenset[str]) -> tuple[str, str]:
    """classify_title плюс жёсткий deny по id — используется и живым
    резолвером (ItemScopeResolver.resolve), и часовой фоновой задачей
    (refresh_item_scope), чтобы правило не разъезжалось по двум копиям."""
    item_id = str(item_id)
    if item_id in hard_deny_ids:
        return DENY, "hard_blocklist"
    return classify_title(title)


@dataclass(frozen=True)
class ItemScopeRow:
    item_id: str
    title: Optional[str]
    decision: str
    reason: str


class ItemScopeStore(Protocol):
    async def get(self, item_id: str) -> Optional[ItemScopeRow]: ...
    async def upsert(self, item_id: str, *, title: Optional[str], decision: str, reason: str) -> None: ...
    async def all(self) -> list[ItemScopeRow]: ...


@dataclass
class InMemoryItemScopeStore:
    """Для тестов и локального прогона — тот же снимок, что держит БД."""

    rows: dict[str, ItemScopeRow] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.rows is None:
            self.rows = {}

    async def get(self, item_id: str) -> Optional[ItemScopeRow]:
        return self.rows.get(str(item_id))

    async def upsert(self, item_id: str, *, title: Optional[str], decision: str, reason: str) -> None:
        self.rows[str(item_id)] = ItemScopeRow(str(item_id), title, decision, reason)

    async def all(self) -> list[ItemScopeRow]:
        return list(self.rows.values())


class SqlAlchemyItemScopeStore:
    """Прод: одна строка на item_id в таблице `item_scope`, перезаписывается
    при повторной классификации — см. докстринг модели ItemScope."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    async def get(self, item_id: str) -> Optional[ItemScopeRow]:
        from sqlalchemy import select

        from app.db.models import ItemScope

        async with self._session_factory() as session:
            row = (
                await session.execute(select(ItemScope).where(ItemScope.item_id == str(item_id)))
            ).scalar_one_or_none()
            if row is None:
                return None
            return ItemScopeRow(row.item_id, row.title, row.decision, row.reason or "")

    async def upsert(self, item_id: str, *, title: Optional[str], decision: str, reason: str) -> None:
        from sqlalchemy import select

        from app.db.models import ItemScope

        item_id = str(item_id)
        async with self._session_factory() as session:
            row = (
                await session.execute(select(ItemScope).where(ItemScope.item_id == item_id))
            ).scalar_one_or_none()
            if row is None:
                session.add(ItemScope(item_id=item_id, title=title, decision=decision, reason=reason))
            else:
                row.title = title
                row.decision = decision
                row.reason = reason
            await session.commit()

    async def all(self) -> list[ItemScopeRow]:
        from sqlalchemy import select

        from app.db.models import ItemScope

        async with self._session_factory() as session:
            rows = (await session.execute(select(ItemScope))).scalars().all()
            return [ItemScopeRow(r.item_id, r.title, r.decision, r.reason or "") for r in rows]


# chat_id-агностичная карточка объявления — то, что нужно резолверу от
# AvitoItemsClient.Listing (item_id, title), без импорта самого dataclass'а,
# чтобы не тянуть модуль ради двух полей.
class _HasItemIdAndTitle(Protocol):
    item_id: str
    title: str


OwnItemIdsProvider = Callable[[], Awaitable[Any]]
ItemCardFetcher = Callable[[str], Awaitable[Optional[_HasItemIdAndTitle]]]

# ФУНКЦИИ ДОГРУЗКИ ОДНОЙ КАРТОЧКИ ЗДЕСЬ НЕТ НАМЕРЕННО. Раньше здесь был
# `make_item_card_fetcher`, делавший СОБСТВЕННЫЙ некешированный
# `list_all_items` на каждый неизвестный item_id — живой прогон против
# аккаунта заказчика (2026-08-28) показал, что классификация десятка своих
# объявлений подряд превращается в десяток запросов к /core/v1/items за
# секунды и упирается в лимит 25/минуту (429 Too Many Requests). Правильный
# источник `fetch_item` — `OwnItemIds.get_listing` (app/avito/own_items.py):
# тот же часовой снимок, которым и так пользуется `own_items_provider`, без
# единого лишнего запроса. См. его докстринг за подробностями находки.


class ItemScopeResolver:
    """Единая классификация item_id -> allow/deny — см. докстринг модуля
    за полным порядком проверок. Вызывается из
    `app/channels/outbound_gate.py:is_listing_allowed`, единственной точки
    фильтрации исходящих (см. её докстринг): новых точек фильтрации этот
    класс не создаёт, он расширяет уже существующую.
    """

    def __init__(
        self,
        store: ItemScopeStore,
        settings: Any,
        *,
        own_items_provider: Optional[OwnItemIdsProvider] = None,
        fetch_item: Optional[ItemCardFetcher] = None,
    ):
        self._store = store
        self._settings = settings
        self._own_items_provider = own_items_provider
        self._fetch_item = fetch_item

    async def resolve(self, item_id: str, *, known_title: Optional[str] = None) -> ItemScopeRow:
        item_id = str(item_id)

        if item_id == ZERO_ITEM_ID:
            return ItemScopeRow(item_id, known_title, DENY, "zero_item_id")

        hard_deny_ids = hard_deny_ids_from_settings(self._settings)
        if item_id in hard_deny_ids:
            # Не кешируем: settings.avito_blocked_items читается заново на
            # каждый вызов намеренно (см. докстринг модуля, пункт 2).
            return ItemScopeRow(item_id, known_title, DENY, "hard_blocklist")

        row = await self._store.get(item_id)
        if row is not None:
            return row

        if self._own_items_provider is not None:
            try:
                own_ids = await self._own_items_provider()
            except Exception:
                # Тот же выбор, что и у OutboundGate.is_allowed: сбой
                # проверки — это запрет, а не разрешение. Молчание лучше,
                # чем прайс на баню в ответ покупателю чужого объявления.
                logger.exception(
                    "item_scope: не удалось получить список своих объявлений — "
                    "%s временно заблокирован",
                    item_id,
                )
                return ItemScopeRow(item_id, known_title, DENY, "own_items_unavailable")
            if item_id not in own_ids:
                row = ItemScopeRow(item_id, known_title, DENY, "not_own_item")
                await self._safe_upsert(row)
                return row

        title = known_title
        fetch_failed = False
        if self._fetch_item is not None:
            try:
                listing = await self._fetch_item(item_id)
            except Exception:
                logger.exception(
                    "item_scope: не удалось догрузить карточку объявления %s", item_id
                )
                fetch_failed = True
            else:
                if listing is not None:
                    title = listing.title

        decision, reason = classify_title(title)
        if fetch_failed:
            return ItemScopeRow(item_id, title, decision, f"fetch_failed:{reason}")

        row = ItemScopeRow(item_id, title, decision, reason)
        await self._safe_upsert(row)
        return row

    async def _safe_upsert(self, row: ItemScopeRow) -> None:
        try:
            await self._store.upsert(row.item_id, title=row.title, decision=row.decision, reason=row.reason)
        except Exception:
            # Сбой записи не должен превращать уже принятое решение в
            # исключение, летящее в OutboundGate, — просто следующий вызов
            # догрузит/классифицирует заново.
            logger.exception("item_scope: не удалось сохранить решение по %s", row.item_id)


@dataclass
class RefreshStats:
    allowed: int = 0
    denied: int = 0

    def record(self, decision: str) -> None:
        if decision == ALLOW:
            self.allowed += 1
        else:
            self.denied += 1

    @property
    def total(self) -> int:
        return self.allowed + self.denied


async def refresh_item_scope(listings: list[Any], store: ItemScopeStore, hard_deny_ids: frozenset[str]) -> RefreshStats:
    """Классифицирует и сохраняет ВСЕ переданные объявления. Отдельно от
    `run_item_scope_refresh_pass` ради теста: здесь нет сети, только чистая
    классификация и запись."""
    stats = RefreshStats()
    for listing in listings:
        decision, reason = classify_listing(listing.item_id, listing.title, hard_deny_ids)
        await store.upsert(listing.item_id, title=listing.title, decision=decision, reason=reason)
        stats.record(decision)
    return stats


async def run_item_scope_refresh_pass(items_client: Any, store: ItemScopeStore, settings: Any) -> RefreshStats:
    """Один час-проход: весь список объявлений аккаунта (те же статусы, что
    и у гуарда поллера — active,old,removed по умолчанию, см.
    `settings.poller_items_statuses`) -> классификация -> запись."""
    statuses = getattr(settings, "poller_items_statuses", "active")
    listings = await items_client.list_all_items(status=statuses)
    hard_deny_ids = hard_deny_ids_from_settings(settings)
    stats = await refresh_item_scope(listings, store, hard_deny_ids)
    logger.info(
        "item_scope: классифицировано %d объявлений — allow %d, deny %d",
        stats.total, stats.allowed, stats.denied,
    )
    return stats
