"""app/channels/item_scope.py — allow/deny по item_id без ручного списка.

Три вещи, ради которых этот модуль появился (см. его докстринг):
классификация по заголовку вместо зашитых id, жёсткий deny поверх любой
классификации, и два класса чатов, найденных пробником живьём: item_id == 0
(109 чатов на 1100) и объявления не нашего аккаунта, где владелец сам
покупатель (33 чата на 1100).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from app.channels.item_scope import (
    ALLOW,
    DENY,
    ZERO_ITEM_ID,
    InMemoryItemScopeStore,
    ItemScopeResolver,
    RefreshStats,
    classify_listing,
    classify_title,
    hard_deny_ids_from_settings,
    refresh_item_scope,
)
from app.config import Settings


@dataclass(frozen=True)
class _Listing:
    item_id: str
    title: str


# --------------------------------------------------------------------------
# classify_title — чистая классификация по заголовку
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "title",
    [
        "Баня на дровах, посуточно",
        "Сдам баню-бочку с купелью",
        "Сауна с бассейном, компания до 10 человек",
        "Гриль-домик с мангалом",
        "Беседка с камином",
        "Юрта для отдыха на природе",
        "Купол прозрачный для фотосессий",
        "Шатёр банкетный",
        "Чан на дровах для купания",
    ],
)
def test_complex_titles_are_allowed(title):
    decision, reason = classify_title(title)
    assert decision == ALLOW
    assert reason == "title_matches_allow"


@pytest.mark.parametrize(
    "title",
    [
        "Требуется менеджер, вакансия открыта",
        "Продам участок 15 соток",
        "Аренда бизнеса под ключ",
        "Квартира-студия у метро",
        "Глэмпинг, домик на природе",
    ],
)
def test_unrelated_titles_are_denied(title):
    decision, reason = classify_title(title)
    assert decision == DENY
    assert reason == "title_matches_deny"


def test_unknown_title_defaults_to_allow():
    """Ни один список не матчит — allow, а не тишина. Тот же принцип, что у
    AVITO_BLOCKED_ITEMS: список deny — основной инструмент, отсутствие
    сигнала — разрешение."""
    decision, reason = classify_title("Подставка для чайника Tefal KI270")
    assert decision == ALLOW
    assert reason == "no_keyword_match"


def test_missing_title_defaults_to_allow():
    decision, reason = classify_title(None)
    assert decision == ALLOW
    assert reason == "no_title"

    decision, reason = classify_title("")
    assert decision == ALLOW
    assert reason == "no_title"


def test_deny_wins_when_a_title_matches_both_lists():
    decision, _ = classify_title("Продажа квартира-студия рядом с баней")
    assert decision == DENY


# --------------------------------------------------------------------------
# Требование 6: жёсткий deny побеждает allow-классификацию ПРИ ЛЮБОМ
# заголовке — включая тот самый живой случай, где чистая классификация по
# словам ошиблась бы: «Продажа банного комплекса» содержит слово «банный».
# --------------------------------------------------------------------------

HARD_DENIED_ITEM = "7980739861"  # продажа банного комплекса — из DEFAULT_BLOCKED_ITEMS

@pytest.mark.parametrize(
    "title",
    [
        "Продажа банного комплекса, участок 15 соток",  # матчит allow-слово "банный"
        "Баня, сауна, парная — весь комплекс целиком",   # матчит сразу три allow-слова
        "Вакансия менеджера по продажам",                 # матчит deny-слово
        "Подставка для чайника Tefal KI270",              # не матчит ничего
        "",
        None,
    ],
)
def test_hard_blocklist_beats_any_title_classification(title):
    hard_deny_ids = hard_deny_ids_from_settings(Settings(avito_blocked_items=HARD_DENIED_ITEM))
    decision, reason = classify_listing(HARD_DENIED_ITEM, title, hard_deny_ids)
    assert decision == DENY
    assert reason == "hard_blocklist"


async def test_resolver_hard_blocklist_beats_any_title_classification():
    """То же самое, но через живой путь ItemScopeResolver.resolve — то, что
    реально вызывает is_listing_allowed."""
    settings = Settings(avito_blocked_items=HARD_DENIED_ITEM)
    resolver = ItemScopeResolver(InMemoryItemScopeStore(), settings)

    for title in ("Баня, сауна, парная — весь комплекс целиком", None, "Подставка для чайника"):
        row = await resolver.resolve(HARD_DENIED_ITEM, known_title=title)
        assert row.decision == DENY
        assert row.reason == "hard_blocklist"


# --------------------------------------------------------------------------
# ItemScopeResolver.resolve — порядок проверок
# --------------------------------------------------------------------------

def _settings(**overrides) -> Settings:
    return Settings(**overrides)


async def test_zero_item_id_is_hard_denied_without_any_lookup():
    """109 чатов на 1100: Авито присылает context.value.id == 0, и
    extract_item_id_from_chat отдаёт для них строку "0" — не None."""
    calls = []

    async def own_items():
        calls.append("own_items")
        return {"0"}  # даже если бы "0" был среди своих объявлений

    store = InMemoryItemScopeStore()
    resolver = ItemScopeResolver(store, _settings(), own_items_provider=own_items)

    row = await resolver.resolve(ZERO_ITEM_ID, known_title="Баня")
    assert row.decision == DENY
    assert row.reason == "zero_item_id"
    assert calls == []            # ни разу не сходили за списком своих объявлений
    assert await store.get("0") is None   # и ничего не закешировали


async def test_foreign_listing_is_hard_denied():
    """33 чата на 1100: владелец аккаунта сам покупатель — «Репетитор по
    физике ЕГЭ», «Покос травы триммером». Заголовок не матчит ни одно
    deny-слово, поэтому без отдельной проверки такой чат классифицировался
    бы как allow."""
    async def own_items():
        return {"111", "222"}   # объявления комплекса — этого среди них нет

    store = InMemoryItemScopeStore()
    resolver = ItemScopeResolver(store, _settings(), own_items_provider=own_items)

    row = await resolver.resolve("999-repetitor", known_title="Репетитор по физике ЕГЭ")
    assert row.decision == DENY
    assert row.reason == "not_own_item"

    cached = await store.get("999-repetitor")
    assert cached is not None and cached.decision == DENY


async def test_own_items_lookup_failure_is_fail_closed_and_not_cached():
    async def broken_own_items():
        raise RuntimeError("Авито недоступен")

    store = InMemoryItemScopeStore()
    resolver = ItemScopeResolver(store, _settings(), own_items_provider=broken_own_items)

    row = await resolver.resolve("555", known_title="Баня")
    assert row.decision == DENY
    assert row.reason == "own_items_unavailable"
    assert await store.get("555") is None   # сбой временный — не кешируем


async def test_known_item_is_read_from_the_table_without_touching_the_network():
    store = InMemoryItemScopeStore()
    await store.upsert("42", title="Баня «Гараж»", decision=ALLOW, reason="title_matches_allow")

    async def own_items():
        raise AssertionError("не должно вызываться — item_id уже в таблице")

    resolver = ItemScopeResolver(store, _settings(), own_items_provider=own_items)
    row = await resolver.resolve("42")
    assert row.decision == ALLOW


async def test_unknown_own_item_is_fetched_classified_and_cached():
    """Новое объявление комплекса, ещё не попавшее в item_scope: живая
    догрузка карточки и классификация по заголовку."""
    async def own_items():
        return {"777"}

    async def fetch_item(item_id: str):
        assert item_id == "777"
        return _Listing(item_id="777", title="Купол прозрачный на крыше")

    store = InMemoryItemScopeStore()
    resolver = ItemScopeResolver(
        store, _settings(), own_items_provider=own_items, fetch_item=fetch_item
    )

    row = await resolver.resolve("777")
    assert row.decision == ALLOW
    assert row.reason == "title_matches_allow"
    assert row.title == "Купол прозрачный на крыше"

    cached = await store.get("777")
    assert cached is not None and cached.decision == ALLOW


async def test_fetch_failure_falls_back_to_known_title_and_is_not_cached():
    """Требование 4: сбой самой догрузки — не повод молчать живому клиенту.
    allow, если уже известный заголовок не матчит deny-слова."""
    async def own_items():
        return {"888"}

    async def broken_fetch(item_id: str):
        raise RuntimeError("Авито 503")

    store = InMemoryItemScopeStore()
    resolver = ItemScopeResolver(
        store, _settings(), own_items_provider=own_items, fetch_item=broken_fetch
    )

    row = await resolver.resolve("888", known_title="Гриль-домик на выходные")
    assert row.decision == ALLOW
    assert row.reason.startswith("fetch_failed:")
    assert await store.get("888") is None   # транзиентный сбой — не кешируем


async def test_fetch_failure_with_a_deny_title_still_denies():
    async def own_items():
        return {"889"}

    async def broken_fetch(item_id: str):
        raise RuntimeError("Авито 503")

    resolver = ItemScopeResolver(
        InMemoryItemScopeStore(), _settings(), own_items_provider=own_items, fetch_item=broken_fetch
    )

    row = await resolver.resolve("889", known_title="Квартира-студия у метро")
    assert row.decision == DENY


async def test_fetch_returning_none_falls_back_to_known_title():
    """Карточка не найдена (снимок не успел обновиться) — не путать со
    сбоем самого запроса: title остаётся тем, что уже знали, кешируем как
    обычно."""
    async def own_items():
        return {"890"}

    async def fetch_item(item_id: str):
        return None

    store = InMemoryItemScopeStore()
    resolver = ItemScopeResolver(
        store, _settings(), own_items_provider=own_items, fetch_item=fetch_item
    )

    row = await resolver.resolve("890", known_title="Баня «Рыцарская»")
    assert row.decision == ALLOW
    assert await store.get("890") is not None


async def test_hard_blocklist_is_reevaluated_live_even_when_cached_as_allow():
    """AVITO_BLOCKED_ITEMS изменили только что — резолвер обязан увидеть
    правку немедленно, не дожидаясь следующего часового прохода
    классификатора, даже если в таблице уже лежит устаревшее allow."""
    store = InMemoryItemScopeStore()
    await store.upsert("42", title="Баня", decision=ALLOW, reason="title_matches_allow")

    resolver = ItemScopeResolver(store, _settings(avito_blocked_items="42"))
    row = await resolver.resolve("42")
    assert row.decision == DENY
    assert row.reason == "hard_blocklist"


async def test_no_own_items_provider_falls_back_to_title_classification():
    """Резолвер без own_items_provider (например, только БД, без Авито) —
    просто классификация по заголовку, без гуарда «объявление не наше»."""
    resolver = ItemScopeResolver(InMemoryItemScopeStore(), _settings())
    row = await resolver.resolve("123", known_title="Сауна с бассейном")
    assert row.decision == ALLOW


# --------------------------------------------------------------------------
# Часовая фоновая классификация
# --------------------------------------------------------------------------

async def test_refresh_item_scope_classifies_and_persists_every_listing():
    listings = [
        _Listing(item_id="1", title="Баня на дровах"),
        _Listing(item_id="2", title="Вакансия менеджера"),
        _Listing(item_id="3", title="Продажа банного комплекса"),  # hard deny
    ]
    store = InMemoryItemScopeStore()
    hard_deny_ids = frozenset({"3"})

    stats = await refresh_item_scope(listings, store, hard_deny_ids)

    assert isinstance(stats, RefreshStats)
    assert stats.allowed == 1
    assert stats.denied == 2
    assert stats.total == 3

    assert (await store.get("1")).decision == ALLOW
    assert (await store.get("2")).decision == DENY
    row3 = await store.get("3")
    assert row3.decision == DENY
    assert row3.reason == "hard_blocklist"
