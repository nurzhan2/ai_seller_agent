"""Application settings. Every secret comes from the environment.

Nothing in this file may carry a real credential as a default — `.env` is
gitignored and `.env.example` holds placeholders only.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Annotated, Literal, Optional

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Avito -----------------------------------------------------------
    avito_client_id: SecretStr = SecretStr("")
    avito_client_secret: SecretStr = SecretStr("")
    avito_user_id: str = ""
    # Заменяет проверку подписи вебхука, алгоритм которой Авито не публикует.
    # Секрет живёт в самом URL: /webhook/avito/{secret}. Минимум 32 символа —
    # см. комментарий в app/channels/avito_endpoints.py.
    avito_webhook_secret: SecretStr = SecretStr("")
    avito_webhook_secret_min_length: int = 32
    # Concurrent in-flight requests to Avito. A semaphore, not a token
    # bucket — see AvitoClient.
    avito_max_concurrency: int = 5
    avito_timeout_seconds: float = 20.0
    avito_max_retries: int = 3

    # --- Anthropic -------------------------------------------------------
    anthropic_api_key: SecretStr = SecretStr("")

    # --- LLM-провайдер (промт №12) ---------------------------------------
    # Заказчик может подключить свой ключ DeepSeek вместо нашего Anthropic —
    # их токены, их расходы. Переключается без пересборки образа, только
    # перезапуском процесса (см. docs/GO_LIVE.md).
    llm_provider: Literal["anthropic", "deepseek"] = "anthropic"
    # Резервный провайдер: включается автоматически после
    # llm_fallback_after_errors подряд идущих ошибок основного
    # (см. app/agent/providers/failover.py). Пусто — резерва нет, ошибка
    # основного провайдера уходит наверх как есть.
    llm_fallback_provider: Optional[Literal["anthropic", "deepseek"]] = None
    llm_fallback_after_errors: int = 3
    # Пусто — имя модели берётся по умолчанию для llm_provider
    # (app.agent.providers.factory.default_models_for).
    llm_dialog_model: str = ""
    llm_classifier_model: str = ""
    # Переопределение base_url — для локальных прокси/тестовых стендов.
    # Пусто — берётся адрес провайдера по умолчанию.
    llm_base_url: str = ""
    deepseek_api_key: SecretStr = SecretStr("")
    # DeepSeek v4 по умолчанию думает (thinking-блок) перед ответом — при
    # маленьком max_tokens это может съесть весь лимит и вернуть пустой
    # ответ (воспроизведено на классификаторе, см. deepseek_provider.py).
    # Выключено по умолчанию — не только защита от этого, но и честное
    # сравнение стоимости/задержки с Anthropic в Части 4 промта №12.
    deepseek_enable_thinking: bool = False

    # --- Админка ---------------------------------------------------------
    # Незаданные значения ОТКЛЮЧАЮТ админку (503), а не открывают её всем.
    admin_user: str = ""
    admin_password: SecretStr = SecretStr("")

    # --- YCLIENTS --------------------------------------------------------
    yclients_partner_token: SecretStr = SecretStr("")
    yclients_user_token: SecretStr = SecretStr("")
    yclients_company_id: str = ""
    # Автобронирование выключено до стабильных метрик модерации.
    auto_booking_enabled: bool = False

    # --- Telegram --------------------------------------------------------
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_ops_chat_id: str = ""
    # NoDecode: без него pydantic-settings пытается сам разобрать это поле
    # как JSON ДО того, как отработает _split_user_ids ниже — и падает
    # SettingsError на пустой строке (`TELEGRAM_ALLOWED_USERS=` без
    # значения), а не на кривом списке. Пустая строка — совершенно рабочее
    # состояние на середине настройки .env, ронять здесь весь Settings()
    # нельзя.
    telegram_allowed_users: Annotated[list[int], NoDecode] = Field(default_factory=list)

    # --- Infrastructure --------------------------------------------------
    database_url: str = "postgresql+asyncpg://parmangal:parmangal@localhost:5432/parmangal"
    redis_url: str = "redis://localhost:6379/0"

    env: Literal["local", "staging", "prod"] = "local"

    # Аварийный рубильник (/pause). Не в .env — переключается из Telegram.
    agent_paused: bool = False
    # После этого числа ответов чат уходит оператору насовсем.
    max_agent_replies_per_chat: int = 25
    debounce_window_seconds: float = 10.0
    daily_cost_limit_rub: Decimal = Decimal("3000")

    # Ships ON. Turning it off means the agent writes to real clients, so it
    # must be a deliberate, explicit act — never a default.
    dry_run: bool = True

    # --- Модерация -----------------------------------------------------
    # Не в .env — переключается из Telegram (/moderation), без передеплоя.
    # DRY_RUN остаётся отдельным аварийным рубильником: пока он включён,
    # ВСЁ уходит на одобрение независимо от этой настройки — она решает,
    # что происходит в живом режиме (dry_run=False).
    #   all             — держать на одобрении всё, как раньше (откат)
    #   concessions_only — одобрение только на ценовую уступку (по умолчанию)
    #   off             — полная автономия, включая ценовые уступки
    moderation_mode: Literal["all", "concessions_only", "off"] = "concessions_only"
    # Сколько ждать реакции оператора на запрос ценовой уступки, прежде чем
    # отправить клиенту версию ответа без скидки и продолжить диалог.
    concession_approval_timeout_minutes: int = 15
    # Как часто фоновый воркер проверяет просроченные запросы на скидку.
    concession_timeout_scheduler_interval_seconds: int = 60

    # Webhook message ids are remembered this long to drop duplicates.
    webhook_idempotency_ttl_seconds: int = 24 * 60 * 60
    # Access token is refreshed this many seconds before it actually expires.
    token_expiry_safety_margin_seconds: int = 60

    # --- Отложенные касания (регламент скидок Максима) -------------------
    # Молчание клиента после названной цены — через столько минут ставится
    # второе касание (мягкое напоминание), потом третье (прямой вопрос).
    touch_reminder_delay_minutes: int = 30
    # Как часто фоновый воркер проверяет диалоги с истёкшим таймером.
    touch_scheduler_interval_seconds: int = 60
    # Максимум напоминаний на диалог — дальше молчим, а не долбим клиента.
    touch_max_count: int = 3

    @field_validator("telegram_allowed_users", mode="before")
    @classmethod
    def _split_user_ids(cls, value: object) -> object:
        """Accept `123,456` from the environment as well as a JSON list."""
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                return stripped
            return [int(part) for part in stripped.split(",") if part.strip()]
        return value

    @field_validator("llm_fallback_provider", mode="before")
    @classmethod
    def _empty_fallback_means_none(cls, value: object) -> object:
        """Пустая строка из .env (`LLM_FALLBACK_PROVIDER=`) — это «не задано»,
        а не невалидное значение литерала. Тот же класс проблемы, что и с
        telegram_allowed_users выше, только для Optional[Literal]."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def require_webhook_secret(self) -> str:
        """Секрет в пути — единственная защита вебхука, поэтому короткий или
        пустой секрет должен ронять запуск, а не молча ослаблять защиту."""
        secret = self.avito_webhook_secret.get_secret_value()
        if len(secret) < self.avito_webhook_secret_min_length:
            raise RuntimeError(
                f"AVITO_WEBHOOK_SECRET короче {self.avito_webhook_secret_min_length} символов. "
                "Сгенерируйте: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
            )
        return secret

    def require_avito_credentials(self) -> tuple[str, str]:
        """Fail loudly rather than sending an empty client_id to Avito."""
        client_id = self.avito_client_id.get_secret_value()
        client_secret = self.avito_client_secret.get_secret_value()
        if not client_id or not client_secret:
            raise RuntimeError(
                "AVITO_CLIENT_ID / AVITO_CLIENT_SECRET are not set — "
                "see .env.example"
            )
        return client_id, client_secret

    def normalized_database_url(self) -> tuple[str, dict]:
        """(url для create_async_engine, connect_args) — см. normalize_database_url."""
        return normalize_database_url(self.database_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def normalize_database_url(raw_url: str) -> tuple[str, dict]:
    """Railway (и Heroku-совместимые платформы) отдают DATABASE_URL со схемой
    `postgres://`/`postgresql://`, а SQLAlchemy с asyncpg требует
    `postgresql+asyncpg://` — без этого создание движка падает на схеме до
    единого запроса к базе.

    Платформа может дополнительно дописать `sslmode=require` в query —
    libpq-параметр, которого asyncpg не понимает как часть DSN: он падает
    с `TypeError`/`invalid connection option "sslmode"` при первом же
    подключении, а не при старте, так что без явной нормализации приложение
    поднимается и тут же перестаёт отвечать на первый запрос к БД. Здесь
    `sslmode` вырезается из строки подключения и переносится в
    `connect_args={"ssl": True}` — так, как ждёт asyncpg.

    Это самая частая причина, по которой приложение не поднимается на
    Railway с первого раза — см. docs/RAILWAY_SETUP.md, п.8.
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(raw_url)
    scheme = parts.scheme
    if scheme in ("postgres", "postgresql"):
        scheme = "postgresql+asyncpg"

    connect_args: dict = {}
    remaining_pairs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "sslmode":
            if value not in ("disable", ""):
                connect_args["ssl"] = True
        else:
            remaining_pairs.append((key, value))

    new_query = urlencode(remaining_pairs)
    normalized = urlunsplit((scheme, parts.netloc, parts.path, new_query, parts.fragment))
    return normalized, connect_args
