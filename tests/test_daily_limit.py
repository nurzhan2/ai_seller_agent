"""app/channels/daily_limit.py — суточный лимит исходящих.

Требование: лимит срабатывает на (N+1)-м сообщении, а не на N-м, и алерт в
Telegram уходит ровно один раз — в момент, когда лимит только что исчерпан,
а не на каждое последующее заблокированное сообщение до конца суток.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.channels import daily_limit


class FakeRedis:
    def __init__(self):
        self.store: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    async def incr(self, key):
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def expire(self, key, ttl):
        self.ttls[key] = ttl
        return True

    async def get(self, key):
        return self.store.get(key)


class BrokenRedis:
    async def incr(self, key):
        raise ConnectionError("Redis недоступен")

    async def expire(self, key, ttl):
        raise ConnectionError("Redis недоступен")


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Основной сценарий: N проходит, N+1 — нет
# --------------------------------------------------------------------------

async def test_messages_up_to_the_limit_are_allowed():
    redis = FakeRedis()
    for _ in range(5):
        result = await daily_limit.check_and_increment(redis, limit=5, now=NOW)
        assert result.allowed is True
        assert result.just_exceeded is False


async def test_the_nplus1th_message_is_blocked_not_the_nth():
    redis = FakeRedis()
    results = [
        await daily_limit.check_and_increment(redis, limit=5, now=NOW) for _ in range(6)
    ]

    fifth = results[4]
    sixth = results[5]

    assert fifth.allowed is True
    assert fifth.count == 5
    assert sixth.allowed is False
    assert sixth.count == 6


async def test_alert_fires_exactly_on_the_nplus1th_message():
    redis = FakeRedis()
    results = [
        await daily_limit.check_and_increment(redis, limit=3, now=NOW) for _ in range(6)
    ]

    just_exceeded_flags = [r.just_exceeded for r in results]
    # Индексы 0..2 — сообщения 1..3 (разрешены), индекс 3 — сообщение 4 (N+1,
    # момент алерта), индексы 4..5 — сообщения 5..6 (уже заблокированы, но
    # БЕЗ повторного алерта).
    assert just_exceeded_flags == [False, False, False, True, False, False]
    assert results[3].count == 4
    assert results[3].limit == 3


async def test_blocked_messages_keep_incrementing_the_counter():
    """Заблокированные попытки тоже считаются — иначе счётчик застрял бы на
    N+1 и алерт срабатывал бы на каждое следующее сообщение снова."""
    redis = FakeRedis()
    for _ in range(4):
        await daily_limit.check_and_increment(redis, limit=2, now=NOW)
    result = await daily_limit.check_and_increment(redis, limit=2, now=NOW)
    assert result.count == 5
    assert result.just_exceeded is False


# --------------------------------------------------------------------------
# Выключен
# --------------------------------------------------------------------------

async def test_limit_zero_disables_the_check_without_touching_redis():
    redis = FakeRedis()
    result = await daily_limit.check_and_increment(redis, limit=0, now=NOW)
    assert result.allowed is True
    assert redis.store == {}


async def test_negative_limit_also_disables_the_check():
    redis = FakeRedis()
    result = await daily_limit.check_and_increment(redis, limit=-1, now=NOW)
    assert result.allowed is True
    assert redis.store == {}


# --------------------------------------------------------------------------
# Сутки — по Москве
# --------------------------------------------------------------------------

async def test_counter_resets_on_the_next_moscow_day():
    redis = FakeRedis()
    late_utc = datetime(2026, 8, 28, 20, 59, tzinfo=timezone.utc)  # 23:59 МСК
    just_after_midnight_utc = datetime(2026, 8, 28, 21, 1, tzinfo=timezone.utc)  # 00:01 МСК след. дня

    for _ in range(3):
        await daily_limit.check_and_increment(redis, limit=3, now=late_utc)
    after_midnight = await daily_limit.check_and_increment(redis, limit=3, now=just_after_midnight_utc)

    assert after_midnight.count == 1
    assert after_midnight.allowed is True


# --------------------------------------------------------------------------
# Redis не настроен вообще — не авария, прозрачно (как kill_switch/dedup)
# --------------------------------------------------------------------------

async def test_no_redis_allows_and_does_not_crash():
    result = await daily_limit.check_and_increment(None, limit=5, now=NOW)
    assert result.allowed is True
    assert result.redis_unavailable is False


# --------------------------------------------------------------------------
# Redis настроен, но упал — fail CLOSED, как и kill switch.
#
# Раньше здесь был fail open: «это счётчик объёма, а не проверка доступа,
# блокировать живых клиентов из-за упавшего Redis хуже». Это рассуждение
# работает против самого смысла лимита — он существует именно на случай
# массовой рассылки/утечки, то есть ровно тогда, когда предохранитель
# нужнее всего. Fail open в аварию означал бы «предохранителя нет именно
# тогда, когда что-то уже пошло не так».
# --------------------------------------------------------------------------

async def test_redis_failure_fails_closed():
    result = await daily_limit.check_and_increment(BrokenRedis(), limit=1, now=NOW)
    assert result.allowed is False
    assert result.redis_unavailable is True


async def test_redis_failure_does_not_crash_the_caller():
    """Исключение из Redis перехватывается и превращается в результат —
    OutboundGate решает, что делать (см. tests/test_outbound_gate.py), а не
    падает само по себе."""
    result = await daily_limit.check_and_increment(BrokenRedis(), limit=10, now=NOW)
    assert result.limit == 10


async def test_redis_failure_is_distinct_from_limit_exceeded():
    """`redis_unavailable` и `just_exceeded` — разные причины блокировки;
    алерт в Telegram обязан различать их (см. app/main.py:
    build_daily_limit_alert), а не говорить «лимит исчерпан» про аварию
    инфраструктуры."""
    result = await daily_limit.check_and_increment(BrokenRedis(), limit=1, now=NOW)
    assert result.redis_unavailable is True
    assert result.just_exceeded is False


# --------------------------------------------------------------------------
# Дефолт настройки: потолок, а не «выключено»
# --------------------------------------------------------------------------
#
# `limit <= 0` выключает проверку целиком (тесты выше), поэтому 0 в дефолте
# означал бы «забыли переменную на деплое — потолка исходящих нет». Ровно
# тот класс ошибки, что уже стоил 65 сообщений: пустой AVITO_BLOCKED_ITEMS и
# незаданный AGENT_MIN_INBOUND_TS читались так же. Дефолт обязан быть
# работающим лимитом, а выключение — осознанным нулём.


def _settings_without_the_variable(monkeypatch):
    from app.config import Settings

    monkeypatch.delenv("OUTBOUND_DAILY_LIMIT", raising=False)
    # _env_file=None: иначе pydantic-settings подхватит .env разработчика и
    # тест начнёт проверять чужую машину вместо дефолта в коде.
    return Settings(_env_file=None)


def test_default_limit_is_a_working_ceiling_not_disabled(monkeypatch):
    settings = _settings_without_the_variable(monkeypatch)

    assert settings.outbound_daily_limit > 0, (
        "дефолт 0 означает «лимита нет» — забытая переменная тихо снимает "
        "потолок исходящих целиком"
    )
    assert settings.outbound_daily_limit == 300


async def test_the_default_actually_blocks_at_its_own_ceiling(monkeypatch):
    """Не только «> 0» на бумаге: дефолт прогоняется через ту же проверку,
    что и живой гейт, и на (N+1)-м сообщении действительно закрывается."""
    settings = _settings_without_the_variable(monkeypatch)
    limit = settings.outbound_daily_limit
    redis = FakeRedis()

    for _ in range(limit):
        assert (await daily_limit.check_and_increment(redis, limit, NOW)).allowed

    over = await daily_limit.check_and_increment(redis, limit, NOW)
    assert over.allowed is False
    assert over.just_exceeded is True


def test_zero_remains_available_as_an_explicit_opt_out(monkeypatch):
    """Ноль не запрещён — он перестал быть ДЕФОЛТОМ. Явно выставленный
    OUTBOUND_DAILY_LIMIT=0 по-прежнему выключает лимит (о чём app/main.py
    кричит WARNING при старте — см. tests/test_main.py)."""
    from app.config import Settings

    monkeypatch.setenv("OUTBOUND_DAILY_LIMIT", "0")
    assert Settings(_env_file=None).outbound_daily_limit == 0
