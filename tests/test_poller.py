"""Поллер: курсоры, холодный старт, дедуп между каналами, пагинация.

Без Авито, Redis и Postgres — фейковый клиент, `InMemoryCursorStore` и
конвейер-обманка. Проверяются ПРАВИЛА (кого будим, кого молчим, куда едет
курсор), а не транспорт: токены, ретраи и лимиты покрыты tests/test_avito.py.

Числа в тестах не выдуманы. 1100 чатов, потолок offset=1000, 33 чата по
чужим объявлениям, 109 чатов с item_id == 0 — это живой аккаунт заказчика,
снятый scripts/poll_once.py до написания поллера.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.avito.cursors import CursorRecord, InMemoryCursorStore
from app.avito.own_items import OwnItemIds
from app.avito.poller import AvitoPoller
from app.channels import avito_payloads as pl
from app.channels import inbound_dedup as dedup
from app.config import Settings
from app.pipeline import MessagePipeline

OUR_USER_ID = "173843599"
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
NOW_TS = int(NOW.timestamp())


def _settings(**overrides) -> Settings:
    base = dict(
        avito_user_id=OUR_USER_ID,
        poller_chats_page_size=2,
        poller_max_offset=1000,
        poller_messages_page_size=2,
        poller_max_message_pages=10,
        poller_interval_seconds=60,
    )
    base.update(overrides)
    return Settings(**base)


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)
        self.ttls.pop(key, None)

    async def expire(self, key, ttl):
        if key in self.store:
            self.ttls[key] = ttl
        return True


class FakePipeline:
    """Считает, что ему подали, и умеет падать на заданном сообщении."""

    def __init__(self, fail_on: set[str] | None = None):
        self.events: list[dict] = []
        self.fail_on = fail_on or set()

    async def handle_message(self, payload, *, source="webhook"):
        message_id = pl.extract_message_id(payload)
        self.events.append(payload)
        return message_id not in self.fail_on

    @property
    def message_ids(self) -> list[str]:
        return [pl.extract_message_id(e) for e in self.events]


class FakeAvito:
    def __init__(self, chats: list[dict], messages: dict[str, list[dict]]):
        self._chats = chats
        self._messages = messages
        self.chat_requests: list[tuple[int, int]] = []
        # offset, после которого API отвечает 400 — как настоящий Авито.
        self.offset_ceiling: int | None = None

    async def list_chats(self, *, limit=50, offset=0):
        self.chat_requests.append((limit, offset))
        if self.offset_ceiling is not None and offset > self.offset_ceiling:
            raise RuntimeError("400 Bad Request")
        return {"chats": self._chats[offset:offset + limit]}

    async def get_messages(self, chat_id, *, limit=50, offset=0):
        batch = self._messages.get(chat_id, [])
        return {"messages": batch[offset:offset + limit]}


def _chat(chat_id, item_id, created, author, title="Баня", message_id=None):
    """`message_id` — id ПОСЛЕДНЕГО сообщения по мнению списка чатов.

    По умолчанию синтетический (`{chat_id}-last`) — большинству тестов
    достаточно самого факта «в чате есть последнее сообщение с таким-то
    created». Передавайте настоящий id явно там, где тест проверяет
    seen_ids после пустого `_fetch_new_messages` (см. app/avito/poller.py:
    `_drain_chat` продвигает курсор ЭТИМ id, если постраничный сборщик
    ничего не вернул) — реальный Авито здесь несёт один и тот же id в
    обоих ответах, а рассинхронизация исключительно тестовая.
    """
    chat: dict = {
        "id": chat_id,
        "last_message": {
            "id": message_id or f"{chat_id}-last", "created": created, "author_id": author,
        },
    }
    if item_id is not None:
        chat["context"] = {"type": "item", "value": {"id": item_id, "title": title}}
    return chat


def _message(message_id, created, author=OUR_USER_ID, text="привет"):
    return {
        "id": message_id,
        "created": created,
        "author_id": author,
        "type": "text",
        "content": {"text": text},
    }


def _poller(chats, messages, *, settings=None, pipeline=None, cursors=None,
            own_items=None, redis=None):
    client = FakeAvito(chats, messages)
    poller = AvitoPoller(
        client=client,
        pipeline=pipeline or FakePipeline(),
        cursors=cursors or InMemoryCursorStore(),
        settings=settings or _settings(),
        redis=redis,
        items_provider=(lambda: _resolved(own_items)) if own_items is not None else None,
        now_fn=lambda: NOW,
    )
    return poller, client


async def _resolved(value):
    return value


# --------------------------------------------------------------------------
# Курсор
# --------------------------------------------------------------------------

async def test_second_pass_feeds_nothing_new():
    chats = [_chat("c1", "111", NOW_TS - 60, "buyer")]
    messages = {"c1": [_message("m1", NOW_TS - 60, author="buyer")]}
    pipeline = FakePipeline()
    cursors = InMemoryCursorStore()

    poller, _ = _poller(chats, messages, pipeline=pipeline, cursors=cursors,
                        own_items={"111"})
    await poller.run_pass()
    assert pipeline.message_ids == ["m1"]

    await poller.run_pass()
    assert pipeline.message_ids == ["m1"], "второй проход подал то же сообщение снова"


async def test_cursor_advances_per_message_not_per_batch():
    """Одно упавшее сообщение не должно уносить курсор за все следующие."""
    created = NOW_TS - 60
    chats = [_chat("c1", "111", created + 2, "buyer")]
    messages = {"c1": [
        _message("m3", created + 2, author="buyer"),
        _message("m2", created + 1, author="buyer"),
        _message("m1", created, author="buyer"),
    ]}
    pipeline = FakePipeline(fail_on={"m2"})
    cursors = InMemoryCursorStore()

    poller, _ = _poller(chats, messages, pipeline=pipeline, cursors=cursors,
                        own_items={"111"})
    await poller.run_pass()

    # m1 обработано, m2 упало — на нём и остановились, m3 не подавали.
    assert pipeline.message_ids == ["m1", "m2"]
    assert cursors.rows["c1"].created == created

    # Следующий проход возвращается ровно к m2, а не начинает заново.
    pipeline.fail_on.clear()
    await poller.run_pass()
    assert pipeline.message_ids == ["m1", "m2", "m2", "m3"]


async def test_new_message_in_the_same_second_as_the_cursor_is_not_lost():
    """РАЗЛИЧАЮЩИЙ СЛУЧАЙ, ради которого существуют и `>=`, и seen_ids.

    Курсор стоит на секунде T, потому что там уже обработано m1. В ту же
    секунду приходит m2. Сравнение `created > курсор` потеряло бы его
    навсегда; сравнение `>=` без списка виденных подало бы заново и m1.
    Верно только вместе.

    Первый проход этого не проверяет: там курсор нулевой, и до ветки со
    списком выполнение не доходит вовсе.
    """
    created = NOW_TS - 60
    chats = [_chat("c1", "111", created, "buyer")]
    messages = {"c1": [
        _message("m2", created, author="buyer"),
        _message("m1", created, author="buyer"),
    ]}
    pipeline = FakePipeline()
    cursors = InMemoryCursorStore()
    # m1 уже обработано ровно на этой секунде.
    cursors.rows["c1"] = CursorRecord("c1", created, ("m1",))

    poller, _ = _poller(chats, messages, pipeline=pipeline, cursors=cursors,
                        own_items={"111"})
    await poller.run_pass()

    assert pipeline.message_ids == ["m2"], "новое сообщение той же секунды потеряно"
    assert set(cursors.rows["c1"].seen_ids) == {"m1", "m2"}


async def test_same_second_messages_are_fed_once_each():
    created = NOW_TS - 60
    chats = [_chat("c1", "111", created, "buyer", message_id="m2")]
    messages = {"c1": [
        _message("m2", created, author="buyer"),
        _message("m1", created, author="buyer"),
    ]}
    pipeline = FakePipeline()
    cursors = InMemoryCursorStore()

    poller, _ = _poller(chats, messages, pipeline=pipeline, cursors=cursors,
                        own_items={"111"})
    await poller.run_pass()
    assert sorted(pipeline.message_ids) == ["m1", "m2"]

    await poller.run_pass()
    assert sorted(pipeline.message_ids) == ["m1", "m2"], "повторная подача"
    assert set(cursors.rows["c1"].seen_ids) == {"m1", "m2"}


async def test_cursor_never_moves_backwards():
    """Сообщение старше курсора не должно откатывать его назад: следующий
    проход подал бы заново весь хвост, который уже обработан."""
    created = NOW_TS - 60
    cursor = CursorRecord("c1", created, ("m5",))
    assert cursor.advanced_by("m1", created - 100) == cursor


# --------------------------------------------------------------------------
# Холодный старт — БЕЗ особого случая (инцидент 2026-08-28, см. докстринг
# app/avito/poller.py и app/avito/cursors.py). Курсор решает только «что
# читать»; «отвечать ли» — AGENT_MIN_INBOUND_TS в конвейере, покрыто
# tests/test_pipeline.py и tests/test_min_inbound_invariant.py.
# --------------------------------------------------------------------------

async def test_cold_start_feeds_an_old_last_message_same_as_any_other_pass():
    """Раньше это была ветка «пропуск: old». Курсору больше нечего решать —
    старое сообщение подаётся в конвейер точно так же, как свежее. Не
    отвечать на него — забота AGENT_MIN_INBOUND_TS, не поллера."""
    old = NOW_TS - 100 * 3600
    chats = [_chat("c1", "111", old, "buyer")]
    messages = {"c1": [_message("m1", old, author="buyer")]}
    pipeline = FakePipeline()
    cursors = InMemoryCursorStore()

    poller, _ = _poller(chats, messages, pipeline=pipeline, cursors=cursors, own_items={"111"})
    await poller.run_pass()

    assert pipeline.message_ids == ["m1"]


async def test_cold_start_feeds_our_own_last_message_too():
    """Раньше это была ветка «пропуск: outgoing_last». Эхо своих же сообщений
    отсекает конвейер (is_outgoing_echo), не поллер — поэтому здесь курсор
    просто подаёт то, что есть."""
    chats = [_chat("c1", "111", NOW_TS - 60, OUR_USER_ID)]
    messages = {"c1": [_message("m1", NOW_TS - 60, author=OUR_USER_ID)]}
    pipeline = FakePipeline()
    cursors = InMemoryCursorStore()

    poller, _ = _poller(chats, messages, pipeline=pipeline, cursors=cursors, own_items={"111"})
    await poller.run_pass()

    assert pipeline.message_ids == ["m1"]


# --------------------------------------------------------------------------
# Гуард «своё ли объявление»
# --------------------------------------------------------------------------

async def test_foreign_listing_is_never_fed():
    """На живом аккаунте 33 таких чата: «Репетитор по физике ЕГЭ», «Покос
    травы триммером» — владелец там покупатель. В чёрном списке их нет."""
    chats = [_chat("c1", "8227028484", NOW_TS - 60, "buyer", "Репетитор по физике ЕГЭ")]
    pipeline = FakePipeline()
    cursors = InMemoryCursorStore()

    poller, _ = _poller(chats, {}, pipeline=pipeline, cursors=cursors,
                        own_items={"111", "222"})
    await poller.run_pass()

    assert pipeline.events == []
    assert cursors.rows["c1"].skipped_reason == "not_our_listing"


async def test_item_id_zero_is_not_treated_as_an_allowed_listing():
    """109 чатов на живом аккаунте: контекст объявления есть, value.id == 0.
    extract_item_id_from_chat отдаёт строку "0", и общий фильтр видит обычное
    разрешённое объявление."""
    chats = [_chat("c1", 0, NOW_TS - 60, "buyer")]
    pipeline = FakePipeline()

    poller, _ = _poller(chats, {}, pipeline=pipeline, own_items={"111"})
    await poller.run_pass()

    assert pipeline.events == []


async def test_pass_is_cancelled_when_listings_are_unavailable():
    """Проверить принадлежность нечем — не трогаем никого. Тот же выбор, что
    у OutboundGate.is_allowed: неудача определения означает запрет."""
    chats = [_chat("c1", "111", NOW_TS - 60, "buyer")]
    pipeline = FakePipeline()

    async def broken():
        raise RuntimeError("Авито недоступно")

    poller = AvitoPoller(
        client=FakeAvito(chats, {}), pipeline=pipeline,
        cursors=InMemoryCursorStore(), settings=_settings(),
        items_provider=broken, now_fn=lambda: NOW,
    )
    await poller.run_pass()
    assert pipeline.events == []


async def test_own_items_survives_a_failed_refresh():
    class Flaky:
        def __init__(self):
            self.calls = 0

        async def list_all_items(self, status="active"):
            self.calls += 1
            if self.calls == 1:
                return [type("L", (), {"item_id": 111})()]
            raise RuntimeError("лимит 25 запросов в минуту")

    clock = {"t": 0.0}
    provider = OwnItemIds(Flaky(), _settings(), monotonic=lambda: clock["t"])

    assert await provider() == {"111"}
    clock["t"] = 10_000.0          # снимок протух, обновление упадёт
    assert await provider() == {"111"}, "устаревший снимок лучше отсутствия"


# --------------------------------------------------------------------------
# Дедуп между каналами — главное, ради чего дедуп переехал в конвейер
# --------------------------------------------------------------------------

async def test_webhook_and_poller_do_not_double_answer(monkeypatch):
    """Одно и то же сообщение из обоих каналов обязано дойти до агента один
    раз. Идентификаторы совпадают — проверено живьём на аккаунте заказчика
    (scripts/poll_once.py --check-ids)."""
    from app.dialog_store import InMemoryDialogStore

    redis = FakeRedis()
    turns: list[str] = []

    class Loop:
        async def run_turn(self, *a, **kw):
            turns.append("turn")
            raise AssertionError("до агента дойти не должно — тест про дедуп")

    pipeline = MessagePipeline(
        store=InMemoryDialogStore(), agent_loop=Loop(), ops_service=None,
        settings=_settings(), redis=redis, debounce_window_seconds=0,
    )

    event = {"payload": {"value": {
        "id": "m-1", "chat_id": "c1", "author_id": "buyer",
        "item_id": 111, "content": {"text": "привет"},
    }}}

    assert await pipeline.handle_message(event, source="webhook") is True
    # Второй канал с тем же message_id — заявка уже занята.
    assert await pipeline.handle_message(event, source="poller") is False
    assert "avito:seen_message:m-1" in redis.store


async def test_failed_handling_releases_the_claim():
    """Упало — сообщение обязано вернуться следующим проходом, а не исчезнуть
    под видом дубля."""
    redis = FakeRedis()

    class Exploding:
        async def get_or_create_chat(self, *a, **kw):
            raise RuntimeError("база недоступна")

    pipeline = MessagePipeline(
        store=Exploding(), agent_loop=None, ops_service=None,
        settings=_settings(), redis=redis, debounce_window_seconds=0,
    )
    event = {"payload": {"value": {
        "id": "m-1", "chat_id": "c1", "author_id": "buyer",
        "item_id": 111, "content": {"text": "привет"},
    }}}

    assert await pipeline.handle_message(event, source="poller") is False
    assert "avito:seen_message:m-1" not in redis.store
    assert await dedup.claim("m-1", redis) is True


# --------------------------------------------------------------------------
# Эквивалентность события — поллер и вебхук обязаны дать одно и то же
# --------------------------------------------------------------------------

def test_built_event_reads_identically_to_a_real_webhook():
    """Если сборщик и разборщики разъедутся, симптомом будет не ошибка, а
    «агент почему-то отвечает не так»."""
    webhook = {"payload": {"value": {
        "id": "m-1", "chat_id": "c1", "author_id": "buyer-9",
        "item_id": 7980589876, "chat_type": "u2i", "type": "text",
        "content": {"text": "сколько стоит баня?"},
    }}}
    built = pl.build_event_from_polled_message(
        _message("m-1", NOW_TS, author="buyer-9", text="сколько стоит баня?"),
        chat_id="c1", item_id="7980589876", chat_type="u2i",
    )

    for extractor in (
        pl.extract_message_id, pl.extract_chat_id, pl.extract_author_id,
        pl.extract_item_id, pl.extract_chat_type, pl.extract_text,
        pl.extract_message_type, pl.is_image_message,
    ):
        assert extractor(built) == extractor(webhook), extractor.__name__


def test_built_event_keeps_item_id_a_string():
    """item_id в API — число, у нас везде строка. Сравнение строки с числом
    молча не совпадает, то есть запрещённое объявление тихо становится
    разрешённым."""
    built = pl.build_event_from_polled_message(
        _message("m-1", NOW_TS), chat_id="c1", item_id=8172444564, chat_type="u2i",
    )
    assert pl.extract_item_id(built) == "8172444564"
    assert isinstance(pl.extract_item_id(built), str)


def test_built_event_omits_item_id_for_chats_without_a_listing():
    """Ключа быть НЕ должно вовсе, а не должен он быть равным None: пустое
    значение читалось бы как строка."""
    built = pl.build_event_from_polled_message(
        _message("m-1", NOW_TS), chat_id="c1", item_id=None, chat_type="u2u",
    )
    assert pl.extract_item_id(built) is None
    assert "item_id" not in built["payload"]["value"]


def test_built_event_survives_an_image_message():
    built = pl.build_event_from_polled_message(
        {"id": "m-1", "created": NOW_TS, "author_id": "b", "type": "image",
         "content": {"image": {"sizes": {}}}},
        chat_id="c1", item_id="111", chat_type="u2i",
    )
    assert pl.is_image_message(built) is True
    assert pl.extract_text(built) is None


# --------------------------------------------------------------------------
# Пагинация и устойчивость
# --------------------------------------------------------------------------

async def test_chat_pagination_walks_every_page():
    chats = [_chat(f"c{i}", "111", NOW_TS - 100 * 3600, "buyer") for i in range(5)]
    cursors = InMemoryCursorStore()

    poller, client = _poller(chats, {}, cursors=cursors, own_items={"111"})
    await poller.run_pass()

    # page_size=2 → страницы по offset 0, 2, 4.
    assert [offset for _limit, offset in client.chat_requests] == [0, 2, 4]
    assert len(cursors.rows) == 5


async def test_offset_ceiling_does_not_kill_the_pass():
    """Живой Авито отвечает 400 на offset=1100. Уже собранные чаты — самые
    свежие, и обработать их лучше, чем не обработать ничего."""
    chats = [_chat(f"c{i}", "111", NOW_TS - 100 * 3600, "buyer") for i in range(6)]
    cursors = InMemoryCursorStore()

    poller, client = _poller(chats, {}, cursors=cursors, own_items={"111"})
    client.offset_ceiling = 2

    stats = await poller.run_pass()
    assert stats.chats_seen == 4
    assert len(cursors.rows) == 4


async def test_message_pagination_reaches_back_to_the_cursor():
    """Сценарий «вернулись после суток простоя»: новых сообщений больше, чем
    помещается на страницу. Молча терять хвост нельзя."""
    base = NOW_TS - 3600
    chats = [_chat("c1", "111", base + 5, "buyer")]
    # v3 отдаёт от свежих к старым.
    messages = {"c1": [_message(f"m{i}", base + i, author="buyer")
                       for i in range(5, 0, -1)]}
    pipeline = FakePipeline()
    cursors = InMemoryCursorStore()
    cursors.rows["c1"] = CursorRecord("c1", base, ("m0",))

    poller, _ = _poller(chats, messages, pipeline=pipeline, cursors=cursors,
                        own_items={"111"})
    await poller.run_pass()

    # page_size=2, а сообщений 5 — все обязаны доехать, по возрастанию.
    assert pipeline.message_ids == ["m1", "m2", "m3", "m4", "m5"]


async def test_truncated_history_is_logged_not_swallowed(caplog):
    base = NOW_TS - 3600
    chats = [_chat("c1", "111", base + 6, "buyer")]
    messages = {"c1": [_message(f"m{i}", base + i, author="buyer")
                       for i in range(6, 0, -1)]}
    cursors = InMemoryCursorStore()
    cursors.rows["c1"] = CursorRecord("c1", base, ("m0",))

    poller, _ = _poller(
        chats, messages,
        settings=_settings(poller_messages_page_size=2, poller_max_message_pages=1),
        cursors=cursors, own_items={"111"},
    )
    with caplog.at_level("WARNING"):
        stats = await poller.run_pass()

    assert stats.truncated_chats == 1
    assert "POLLER_MAX_MESSAGE_PAGES" in caplog.text


async def test_one_bad_chat_does_not_stop_the_others():
    chats = [
        _chat("bad", "111", NOW_TS - 60, "buyer"),
        _chat("good", "111", NOW_TS - 60, "buyer"),
    ]
    messages = {"good": [_message("m1", NOW_TS - 60, author="buyer")]}
    pipeline = FakePipeline()

    class HalfBroken(FakeAvito):
        async def get_messages(self, chat_id, *, limit=50, offset=0):
            if chat_id == "bad":
                raise RuntimeError("таймаут")
            return await FakeAvito.get_messages(self, chat_id, limit=limit, offset=offset)

    poller = AvitoPoller(
        client=HalfBroken(chats, messages), pipeline=pipeline,
        cursors=InMemoryCursorStore(), settings=_settings(),
        items_provider=lambda: _resolved({"111"}), now_fn=lambda: NOW,
    )
    stats = await poller.run_pass()

    assert stats.failed_chats == 1
    assert pipeline.message_ids == ["m1"]


# --------------------------------------------------------------------------
# Захват
# --------------------------------------------------------------------------

async def test_second_replica_skips_the_pass():
    chats = [_chat("c1", "111", NOW_TS - 60, "buyer")]
    messages = {"c1": [_message("m1", NOW_TS - 60, author="buyer")]}
    redis = FakeRedis()

    first, _ = _poller(chats, messages, own_items={"111"}, redis=redis)
    await first._acquire_lock()          # первая реплика ещё в проходе

    pipeline = FakePipeline()
    second, _ = _poller(chats, messages, pipeline=pipeline, own_items={"111"},
                        redis=redis)
    await second.run_pass()

    assert pipeline.events == []


async def test_lost_lock_stops_cursor_writes():
    """Захват протух на середине прохода — писать курсоры нельзя: их уже
    пишет другая реплика."""
    chats = [_chat("c1", "111", NOW_TS - 60, "buyer")]
    messages = {"c1": [_message("m1", NOW_TS - 60, author="buyer")]}
    redis = FakeRedis()
    cursors = InMemoryCursorStore()

    poller, _ = _poller(chats, messages, cursors=cursors, own_items={"111"},
                        redis=redis)

    original = poller._save_cursor

    async def steal_then_save(record):
        redis.store["avito:poller:lock"] = "чужой-токен"
        return await original(record)

    poller._save_cursor = steal_then_save
    await poller.run_pass()

    assert cursors.rows == {}
