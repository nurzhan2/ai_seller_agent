"""SQLAlchemy 2.x models.

The one that matters most is `DialogState`: it is the persistent form of
`app.pricing.concessions.DialogConcessionState`. Without it the ratchet and
the concession ladder live only in process memory, so a restart would let
the agent re-quote a price *above* one it already promised — the exact leak
app/pricing/quote_gate.py exists to prevent.
"""

from __future__ import annotations

import enum
from datetime import date as DateType, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Direction(str, enum.Enum):
    incoming = "incoming"      # from the client
    outgoing = "outgoing"      # from us (agent or operator)


class SendStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    dry_run = "dry_run"        # composed but deliberately not delivered
    failed = "failed"
    rejected = "rejected"      # operator declined it in moderation


class Author(str, enum.Enum):
    client = "client"
    agent = "agent"
    operator = "operator"


class ChatState(str, enum.Enum):
    new = "new"
    qualifying = "qualifying"
    quoted = "quoted"
    lead_captured = "lead_captured"
    escalated = "escalated"
    closed = "closed"


class Chat(Base):
    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    item_id: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    # Resolved via ItemZoneMap — which listing the client came from decides
    # which zone we talk about.
    zone_id: Mapped[Optional[str]] = mapped_column(String(64))
    buyer_name: Mapped[Optional[str]] = mapped_column(String(255))
    state: Mapped[ChatState] = mapped_column(
        SAEnum(ChatState, name="chat_state"), default=ChatState.new
    )
    # Operator took the wheel; the agent stays silent until this clears.
    is_human_takeover: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    agent_reply_count: Mapped[int] = mapped_column(Integer, default=0)
    takeover_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_msg_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    messages: Mapped[list["Message"]] = relationship(back_populates="chat")
    dialog_state: Mapped[Optional["DialogState"]] = relationship(
        back_populates="chat", uselist=False
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        # Idempotency backstop: Redis drops duplicate webhooks, but a unique
        # index means a Redis flush cannot produce doubled messages.
        UniqueConstraint("avito_message_id", name="uq_messages_avito_message_id"),
        Index("ix_messages_chat_created", "chat_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("chats.chat_id", ondelete="CASCADE"), index=True
    )
    direction: Mapped[Direction] = mapped_column(SAEnum(Direction, name="direction"))
    author: Mapped[Author] = mapped_column(
        SAEnum(Author, name="author"), default=Author.client
    )
    text: Mapped[Optional[str]] = mapped_column(Text)
    image_ids: Mapped[Optional[list[str]]] = mapped_column(ARRAY(String))
    avito_message_id: Mapped[Optional[str]] = mapped_column(String(128))
    status: Mapped[SendStatus] = mapped_column(
        SAEnum(SendStatus, name="send_status"), default=SendStatus.pending
    )
    # {"provider": ..., "model": ..., "input_tokens": ..., "output_tokens": ...,
    #  "cost_rub": ..., "cached_input_tokens": ...}
    llm_meta: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    chat: Mapped["Chat"] = relationship(back_populates="messages")


class DialogState(Base):
    """Persistent DialogConcessionState — see module docstring."""

    __tablename__ = "dialog_states"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("chats.chat_id", ondelete="CASCADE"), unique=True, index=True
    )
    base_price_quoted: Mapped[bool] = mapped_column(Boolean, default=False)
    used_tiers: Mapped[Optional[list[int]]] = mapped_column(ARRAY(Integer), default=list)
    skipped_tiers: Mapped[Optional[list[int]]] = mapped_column(ARRAY(Integer), default=list)
    # Numeric, never float — the whole pricing stack is Decimal end to end.
    floor_reached: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    client_constraints: Mapped[Optional[list[str]]] = mapped_column(ARRAY(String), default=list)
    concessions_count: Mapped[int] = mapped_column(Integer, default=0)

    # --- Отложенные касания (регламент скидок Максима) --------------------
    # В БД, не в памяти процесса — переживает рестарт контейнера. Один и тот
    # же движок для 30-минутного напоминания и для min_touches в скидках:
    # touch_count растёт только когда касание РЕАЛЬНО отправлено (первое —
    # когда назвали цену, второе и третье — воркером), а не когда оно
    # запланировано.
    touch_count: Mapped[int] = mapped_column(Integer, default=0)
    last_touch_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # NULL означает «таймер не тикает» — либо касаний ещё не было, либо
    # клиент только что ответил (см. app.agent.touch_tracking.reset_timer_on_reply),
    # либо лимит touch_max_count уже исчерпан.
    next_touch_due_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    chat: Mapped["Chat"] = relationship(back_populates="dialog_state")

    def to_runtime(self):
        """Rehydrate into the frozen dataclass the concession engine uses."""
        from app.pricing.concessions import DialogConcessionState

        return DialogConcessionState(
            base_price_quoted=self.base_price_quoted,
            used_tiers=frozenset(self.used_tiers or ()),
            floor_reached=self.floor_reached,
            touch_count=self.touch_count,
        )


class ItemZoneMap(Base):
    """Which Avito listing corresponds to which zone.

    The audit found this mapping is genuinely incomplete on the client's
    side: bath «Гараж» has at least two separate listings, bath «Рыцарская»
    has none, and no dome listing was found at all (question 12.2). Worse,
    the client confirmed (prompt 11, part 3) that baths in particular SHARE
    ad slots — one listing, several possible baths behind it.

    `zone_id` is set when the listing maps to exactly one zone. `category`
    is set instead when only the zone *category* is known (a bath ad, but
    which of the three baths is ambiguous from the ad alone) — the agent
    then asks one disambiguating question naming just the zones in that
    category, per app.agent.listing_context.build_listing_hint. A row with
    neither set, or no row at all, means "ask the client / fall back to the
    site link" — never a guess.
    """

    __tablename__ = "item_zone_map"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    item_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    zone_id: Mapped[Optional[str]] = mapped_column(String(64))
    category: Mapped[Optional[str]] = mapped_column(String(32))
    # Заголовок объявления на Авито — только чтобы оператор в /admin/dialogs
    # видел, откуда пришёл клиент, словами, а не голым числом item_id.
    # Заполняется `python -m scripts.export_listings --seed-map`; ни на одно
    # решение агента не влияет и влиять не должно.
    title: Mapped[Optional[str]] = mapped_column(String(512))
    note: Mapped[Optional[str]] = mapped_column(Text)


class ConcessionLog(Base):
    """One row per concession decision — granting AND denying (rule R12).

    Columns mirror concessions.yaml → logging.fields, plus the two fields
    added in prompt 3.2 so the operator can tell whose rule produced the
    discount and what the loss was measured against.
    """

    __tablename__ = "concession_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dialog_id: Mapped[str] = mapped_column(String(128), index=True)
    zone: Mapped[Optional[str]] = mapped_column(String(64))
    tier: Mapped[Optional[int]] = mapped_column(Integer)
    trigger: Mapped[Optional[str]] = mapped_column(String(255))
    base_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    final_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    revenue_delta: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    revenue_delta_basis: Mapped[Optional[str]] = mapped_column(String(32))
    exchange_given: Mapped[Optional[str]] = mapped_column(String(64))
    allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    denial_reason: Mapped[Optional[str]] = mapped_column(Text)
    # True when the grant rested on a threshold WE set provisionally rather
    # than one the client confirmed (question 13.4).
    provisional_policy: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    name: Mapped[Optional[str]] = mapped_column(String(255))
    phone: Mapped[Optional[str]] = mapped_column(String(64))
    zone_id: Mapped[Optional[str]] = mapped_column(String(64))
    date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    guests: Mapped[Optional[int]] = mapped_column(Integer)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Booking(Base):
    """Бронь, поставленную агентом, пишем У СЕБЯ, а не только в YCLIENTS.

    Своя запись нужна не для дублирования календаря, а чтобы был ответ на
    вопрос «что агент вообще набронировал» в тот момент, когда YCLIENTS
    недоступен, отдал ошибку на середине или когда бронь ставили и
    отменяли. `record_id` — идентификатор на стороне YCLIENTS (может быть
    пустым, если тот ответил успехом, но id не вернул).

    `occupied_hours` и `billable_hours` хранятся ОБА и намеренно
    различаются: при акции «6-й час в подарок» гость занимает площадку 6
    часов, а платит за 5. В YCLIENTS блокируются занятые, в деньгах
    считаются оплаченные, и увидеть в записи только одно число означало
    бы однажды перепутать их местами.
    """

    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    record_id: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    zone_id: Mapped[Optional[str]] = mapped_column(String(64))
    booking_date: Mapped[Optional[DateType]] = mapped_column(Date)
    start_time: Mapped[Optional[str]] = mapped_column(String(8))
    occupied_hours: Mapped[Optional[int]] = mapped_column(Integer)
    billable_hours: Mapped[Optional[int]] = mapped_column(Integer)
    guests: Mapped[Optional[int]] = mapped_column(Integer)
    total: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    client_name: Mapped[Optional[str]] = mapped_column(String(255))
    client_phone: Mapped[Optional[str]] = mapped_column(String(64))
    applied_promo: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PendingReplyRow(Base):
    """Ответ агента, ждущий кнопки оператора в DRY_RUN.

    В БД, а не в памяти процесса: до этого очередь модерации жила в
    `InMemoryOpsStore`, и рестарт контейнера (любой редеплой на Railway)
    молча терял всё, что оператор не успел одобрить. Клиент при этом уже
    написал и ждёт — а ответ, который для него подготовили, исчезал вместе
    с процессом.

    Одна ожидающая реплика на чат — `chat_id` уникален. Это ровно та
    семантика, что была у словаря: новый ответ агента вытесняет
    неодобренный предыдущий, потому что отвечать на позавчерашнюю реплику
    клиента уже поздно.
    """

    __tablename__ = "pending_replies"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    text: Mapped[str] = mapped_column(Text)
    # pending | approved | rejected | edited — строкой, а не Enum: статусы
    # модерации живут в app/ops/state.py и меняются чаще, чем стоит платить
    # миграцией за каждое новое значение.
    status: Mapped[str] = mapped_column(String(32), default="pending")
    decided_by: Mapped[Optional[int]] = mapped_column(BigInteger)
    # --- Модерация ценовых уступок (MODERATION_MODE=concessions_only) ----
    # is_concession: этот pending — запрос на скидку, а не обычный
    # DRY_RUN-холд. У него есть дедлайн (due_at); просроченный воркер
    # (app.pipeline.MessagePipeline.check_concession_timeouts) отправляет
    # клиенту fallback_text — предпосчитанный ответ БЕЗ скидки — вместо
    # того, чтобы держать диалог в тишине, пока оператор не освободится.
    is_concession: Mapped[bool] = mapped_column(Boolean, default=False)
    fallback_text: Mapped[Optional[str]] = mapped_column(Text)
    due_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CatalogOverride(Base):
    """Правка каталога поверх YAML — цена, график, вместимость.

    Существует потому, что файловая система контейнера на Railway
    эфемерная: запись в `app/kb/catalog.yaml` пережила бы ровно до
    следующего деплоя и исчезла бы МОЛЧА. YAML остаётся базой (он в git,
    он ревьюится), БД — слоем изменений поверх него.

    Строки не удаляются и не переписываются: каждая правка добавляет новую,
    последняя по времени для того же `path` побеждает. Это и журнал («кто,
    когда, что было, что стало»), и механика отката — откат помечает
    `reverted_at`, а не стирает историю.
    """

    __tablename__ = "catalog_overrides"
    __table_args__ = (
        # Активные правки читаются на каждом старте и после каждой правки —
        # это единственный запрос к таблице в горячем пути.
        Index("ix_catalog_overrides_active", "reverted_at", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # Путь в документе, формат — см. app/kb/overrides.py
    # ($.catalog.zones[id=dome_bags].pricing.weekend_per_hour)
    path: Mapped[str] = mapped_column(String(512), index=True)
    # Новое значение, JSON-совместимое: скаляр, список или узел
    # DisputedValue целиком. JSONB, а не строка: значение бывает и списком
    # (праздничные даты), и словарём, и числом.
    value: Mapped[Any] = mapped_column(JSONB)
    # Что было до правки — только для журнала и для человекочитаемого
    # «было X, станет Y». Источник истины для отката — не это поле, а
    # отсутствие строки: откат помечает reverted_at, и предыдущая правка
    # того же пути (или сам YAML) снова становится действующей.
    previous_value: Mapped[Optional[Any]] = mapped_column(JSONB)
    # Ключ поля из app/kb/editable.py — чтобы журнал показывал «Выходные,
    # ₽/час», а не только путь.
    field_key: Mapped[Optional[str]] = mapped_column(String(64))
    zone_id: Mapped[Optional[str]] = mapped_column(String(64))
    changed_by: Mapped[int] = mapped_column(BigInteger)
    comment: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    reverted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    reverted_by: Mapped[Optional[int]] = mapped_column(BigInteger)


class OperatorAction(Base):
    """Audit trail for the Telegram controls (prompt 6 uses this)."""

    __tablename__ = "operator_actions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    chat_id: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    telegram_user_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(64))
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
