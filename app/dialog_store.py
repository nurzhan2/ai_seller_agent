"""Хранилище диалога для конвейера обработки входящих.

Тот же приём, что у `OpsStore` и `TouchStore`: протокол + `InMemory...` для
тестов + `SqlAlchemy...` для прода. Конвейер (app/pipeline.py) не знает, где
лежат данные, поэтому сквозной путь «вебхук → агент → ответ» проверяется
тестами без поднятого Postgres.

Отдельным модулем, а не внутри app/pipeline.py: конвейер — это правила
(кого не трогать, когда сбросить таймер, что отправить), а это — доступ к
данным. Смешивать их в одном файле означает, что правила нельзя прочитать,
не пролистав SQL.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Protocol

from app.agent.listing_context import ItemZoneRow
from app.agent.touch_tracking import TouchState
from app.db.models import Author, Direction, SendStatus
from app.pricing.concessions import ConcessionEvent, DialogConcessionState

logger = logging.getLogger("parmangal.dialog_store")

# «Сегодня» для дневного лимита уступок (R10) считается по Москве — там
# работает комплекс, — а не по UTC: полночь UTC приходится на 03:00 по
# Москве, и граница дня уехала бы на три часа назад. Фиксированный сдвиг,
# а не zoneinfo("Europe/Moscow") — Россия не переходит на летнее время с
# 2014 года, а zoneinfo на python:3.12-slim требует отдельно поставленного
# пакета tzdata (не факт, что стоит в контейнере) ради того, что и так
# не меняется.
MOSCOW_TZ = timezone(timedelta(hours=3), name="MSK")

# Сколько сообщений диалога поднимаем из БД в контекст модели. Совпадает с
# HISTORY_WINDOW в app/agent/loop.py — там же лишнее ещё раз обрежется, но
# грузить из базы заведомо больше, чем модель увидит, незачем.
HISTORY_LIMIT = 30

# Сколько последних наших исходящих сравнивать с эхом, решая «это мы или
# живой менеджер» (app/pipeline.py, ветка эха). Авито присылает эхо в
# пределах секунд после отправки, так что глубина здесь — запас на случай
# отставания поллера, а не история диалога: на длинном хвосте растёт только
# шанс совпасть с повторно отправленным тем же текстом.
ECHO_LOOKBACK = 20

# Статусы, которые НЕ попадают в историю для модели: этих текстов клиент не
# видел. Показать их модели — значит убедить её, что она уже ответила, и
# получить ответ на несуществующую реплику. `dry_run`/`pending` при этом
# остаются: в режиме модерации мы намеренно проигрываем диалог так, как если
# бы ответ ушёл (иначе агента в DRY_RUN невозможно оценить на длинном
# диалоге — он каждый раз начинал бы с нуля).
HISTORY_EXCLUDED_STATUSES = (SendStatus.rejected, SendStatus.failed)


@dataclass(frozen=True)
class ChatRecord:
    """Снимок строки `Chat` — ровно те поля, которые нужны конвейеру."""

    chat_id: str
    item_id: Optional[str] = None
    zone_id: Optional[str] = None
    buyer_name: Optional[str] = None
    is_human_takeover: bool = False
    ai_enabled: bool = True
    manual_hold: bool = False
    agent_reply_count: int = 0


class DialogStore(Protocol):
    async def get_or_create_chat(
        self, chat_id: str, item_id: Optional[str] = None, buyer_name: Optional[str] = None
    ) -> ChatRecord: ...

    async def save_incoming(
        self, chat_id: str, text: str, avito_message_id: Optional[str] = None
    ) -> bool: ...

    async def save_outgoing(
        self,
        chat_id: str,
        text: str,
        status: SendStatus,
        llm_meta: Optional[dict] = None,
        author: Author = Author.agent,
    ) -> None: ...

    async def load_history(self, chat_id: str, limit: int = HISTORY_LIMIT) -> list[dict]: ...

    async def load_dialog_state(
        self, chat_id: str
    ) -> tuple[DialogConcessionState, TouchState]: ...

    async def save_dialog_state(
        self, chat_id: str, concession: DialogConcessionState, touch: TouchState
    ) -> None: ...

    async def bump_agent_reply_count(self, chat_id: str) -> None: ...

    # Только чтение, БЕЗ создания чата: нужен `OutboundGate`, чтобы решить,
    # имеем ли мы право писать в этот чат. `get_or_create_chat` тут не
    # годится — проверка перед отправкой не должна заводить строку в базе.
    async def get_chat_item_id(self, chat_id: str) -> Optional[str]: ...

    async def get_chat_manual_hold(self, chat_id: str) -> bool: ...

    async def set_chat_manual_hold(self, chat_id: str, value: bool) -> bool: ...

    async def was_sent_by_us(self, chat_id: str, text: str) -> bool: ...

    async def last_incoming_at(self, chat_id: str) -> Optional[datetime]: ...

    async def get(self, item_id: str) -> Optional[ItemZoneRow]: ...

    async def log_concession(self, chat_id: str, event: ConcessionEvent) -> None: ...

    async def count_concessions_today(self, now: Optional[datetime] = None) -> int: ...


# --------------------------------------------------------------------------
# Реализация для тестов
# --------------------------------------------------------------------------

@dataclass
class InMemoryDialogStore:
    """Тот же интерфейс, только в словарях процесса.

    Умышленно повторяет и неудобные свойства настоящего хранилища —
    в частности, `save_incoming` так же возвращает False на повторный
    `avito_message_id`, потому что сквозной тест дедупликации обязан
    проверять именно это поведение, а не его упрощение.
    """

    chats: dict[str, ChatRecord] = field(default_factory=dict)
    messages: dict[str, list[dict]] = field(default_factory=dict)
    concessions: dict[str, DialogConcessionState] = field(default_factory=dict)
    touches: dict[str, TouchState] = field(default_factory=dict)
    item_zones: dict[str, ItemZoneRow] = field(default_factory=dict)
    seen_message_ids: set[str] = field(default_factory=set)
    concession_log: list[dict] = field(default_factory=list)

    async def get_or_create_chat(
        self, chat_id: str, item_id: Optional[str] = None, buyer_name: Optional[str] = None
    ) -> ChatRecord:
        existing = self.chats.get(chat_id)
        if existing is not None:
            # item_id/buyer_name дозаполняются, если в первом вебхуке их не
            # было, а в следующем появились — но НЕ затираются на None.
            updated = ChatRecord(
                chat_id=chat_id,
                item_id=existing.item_id or item_id,
                zone_id=existing.zone_id or self._zone_for(item_id or existing.item_id),
                buyer_name=existing.buyer_name or buyer_name,
                is_human_takeover=existing.is_human_takeover,
                ai_enabled=existing.ai_enabled,
                manual_hold=existing.manual_hold,
                agent_reply_count=existing.agent_reply_count,
            )
            self.chats[chat_id] = updated
            return updated

        record = ChatRecord(
            chat_id=chat_id,
            item_id=item_id,
            zone_id=self._zone_for(item_id),
            buyer_name=buyer_name,
        )
        self.chats[chat_id] = record
        return record

    def _zone_for(self, item_id: Optional[str]) -> Optional[str]:
        if item_id is None:
            return None
        row = self.item_zones.get(item_id)
        return row.zone_id if row is not None else None

    async def save_incoming(
        self, chat_id: str, text: str, avito_message_id: Optional[str] = None
    ) -> bool:
        if avito_message_id is not None:
            if avito_message_id in self.seen_message_ids:
                return False
            self.seen_message_ids.add(avito_message_id)
        self.messages.setdefault(chat_id, []).append(
            {
                "direction": Direction.incoming,
                "author": Author.client,
                "text": text,
                "status": SendStatus.sent,
                "avito_message_id": avito_message_id,
                "llm_meta": None,
                # Нужен `last_incoming_at`: по нему пути, отправляющие не в
                # ответ на входящее (касания, запасной ответ по таймауту
                # уступки), проверяют AGENT_MIN_INBOUND_TS. В БД это
                # серверный DEFAULT now(), здесь ставим руками.
                "created_at": datetime.now(timezone.utc),
            }
        )
        return True

    async def save_outgoing(
        self,
        chat_id: str,
        text: str,
        status: SendStatus,
        llm_meta: Optional[dict] = None,
        author: Author = Author.agent,
    ) -> None:
        self.messages.setdefault(chat_id, []).append(
            {
                "direction": Direction.outgoing,
                "author": author,
                "text": text,
                "status": status,
                "avito_message_id": None,
                "llm_meta": llm_meta,
            }
        )

    async def load_history(self, chat_id: str, limit: int = HISTORY_LIMIT) -> list[dict]:
        rows = [
            m for m in self.messages.get(chat_id, [])
            if m["status"] not in HISTORY_EXCLUDED_STATUSES
        ]
        return [
            {
                "role": "user" if m["direction"] == Direction.incoming else "assistant",
                "content": m["text"] or "",
            }
            for m in rows[-limit:]
        ]

    async def load_dialog_state(
        self, chat_id: str
    ) -> tuple[DialogConcessionState, TouchState]:
        return (
            self.concessions.get(chat_id, DialogConcessionState()),
            self.touches.get(chat_id, TouchState()),
        )

    async def save_dialog_state(
        self, chat_id: str, concession: DialogConcessionState, touch: TouchState
    ) -> None:
        self.concessions[chat_id] = concession
        self.touches[chat_id] = touch

    async def bump_agent_reply_count(self, chat_id: str) -> None:
        existing = self.chats.get(chat_id)
        if existing is None:
            return
        self.chats[chat_id] = ChatRecord(
            chat_id=existing.chat_id,
            item_id=existing.item_id,
            zone_id=existing.zone_id,
            buyer_name=existing.buyer_name,
            is_human_takeover=existing.is_human_takeover,
            ai_enabled=existing.ai_enabled,
            agent_reply_count=existing.agent_reply_count + 1,
        )

    async def get_chat_item_id(self, chat_id: str) -> Optional[str]:
        existing = self.chats.get(chat_id)
        return existing.item_id if existing else None

    async def get_chat_manual_hold(self, chat_id: str) -> bool:
        existing = self.chats.get(chat_id)
        return existing.manual_hold if existing else False

    async def set_chat_manual_hold(self, chat_id: str, value: bool) -> bool:
        # `ChatRecord` — frozen: правится заменой строки целиком, как и
        # везде в этом сторе (см. get_or_create_chat выше). Мутация поля
        # молча роняла бы команду /hold, а она ставится по инциденту.
        import dataclasses

        chat = await self.get_or_create_chat(chat_id)
        self.chats[chat_id] = dataclasses.replace(chat, manual_hold=value)
        return value

    async def was_sent_by_us(self, chat_id: str, text: str) -> bool:
        normalized = (text or "").strip()
        if not normalized:
            return False
        recent = [
            m for m in self.messages.get(chat_id, [])
            if m["direction"] == Direction.outgoing and m["author"] == Author.agent
        ][-ECHO_LOOKBACK:]
        return any((m["text"] or "").strip() == normalized for m in recent)

    async def last_incoming_at(self, chat_id: str) -> Optional[datetime]:
        stamps = [
            m["created_at"] for m in self.messages.get(chat_id, [])
            if m["direction"] == Direction.incoming and m.get("created_at") is not None
        ]
        return max(stamps) if stamps else None

    async def get(self, item_id: str) -> Optional[ItemZoneRow]:
        return self.item_zones.get(item_id)

    async def log_concession(self, chat_id: str, event: ConcessionEvent) -> None:
        self.concession_log.append(
            {"chat_id": chat_id, "event": event, "at": datetime.now(timezone.utc)}
        )

    async def count_concessions_today(self, now: Optional[datetime] = None) -> int:
        today_msk = (now or datetime.now(MOSCOW_TZ)).astimezone(MOSCOW_TZ).date()
        return sum(
            1
            for row in self.concession_log
            if row["event"].decision.allowed and row["at"].astimezone(MOSCOW_TZ).date() == today_msk
        )


# --------------------------------------------------------------------------
# Реализация для прода
# --------------------------------------------------------------------------

class SqlAlchemyDialogStore:
    """Одна сессия на вызов — так же, как `SqlAlchemyTouchStore` и
    `SqlAlchemyZoneMapping`: сессии не живут между шагами конвейера, потому
    что между ними есть окно debounce длиной в десятки секунд, а держать
    соединение открытым всё это время незачем."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    async def get_or_create_chat(
        self, chat_id: str, item_id: Optional[str] = None, buyer_name: Optional[str] = None
    ) -> ChatRecord:
        from sqlalchemy import select

        from app.db.models import Chat, ItemZoneMap

        async with self._session_factory() as session:
            chat = (
                await session.execute(select(Chat).where(Chat.chat_id == chat_id))
            ).scalar_one_or_none()

            if chat is None:
                chat = Chat(chat_id=chat_id, item_id=item_id, buyer_name=buyer_name)
                session.add(chat)
            else:
                # Только дозаполнение: пришедший без item_id вебхук не должен
                # стирать связь с объявлением, установленную раньше.
                if item_id and not chat.item_id:
                    chat.item_id = item_id
                if buyer_name and not chat.buyer_name:
                    chat.buyer_name = buyer_name

            if chat.zone_id is None and chat.item_id:
                row = (
                    await session.execute(
                        select(ItemZoneMap).where(ItemZoneMap.item_id == chat.item_id)
                    )
                ).scalar_one_or_none()
                if row is not None and row.zone_id:
                    chat.zone_id = row.zone_id

            chat.last_msg_at = datetime.now(timezone.utc)
            await session.commit()

            return ChatRecord(
                chat_id=chat.chat_id,
                item_id=chat.item_id,
                zone_id=chat.zone_id,
                buyer_name=chat.buyer_name,
                is_human_takeover=chat.is_human_takeover,
                ai_enabled=chat.ai_enabled,
                manual_hold=chat.manual_hold,
                agent_reply_count=chat.agent_reply_count,
            )

    async def save_incoming(
        self, chat_id: str, text: str, avito_message_id: Optional[str] = None
    ) -> bool:
        """False означает «это сообщение уже сохранено» — второй рубеж
        дедупликации после Redis (`app/webhooks.py`). Именно уникальный
        индекс, а не SELECT перед INSERT: два воркера, получившие один ретрай
        одновременно, оба увидели бы «нет такого» и оба вставили бы строку.
        """
        from sqlalchemy.exc import IntegrityError

        from app.db.models import Message

        async with self._session_factory() as session:
            session.add(
                Message(
                    chat_id=chat_id,
                    direction=Direction.incoming,
                    author=Author.client,
                    text=text,
                    avito_message_id=avito_message_id,
                    status=SendStatus.sent,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                logger.info(
                    "duplicate incoming message dropped by the unique index",
                    extra={"chat_id": chat_id, "avito_message_id": avito_message_id},
                )
                return False
        return True

    async def save_outgoing(
        self,
        chat_id: str,
        text: str,
        status: SendStatus,
        llm_meta: Optional[dict] = None,
        author: Author = Author.agent,
    ) -> None:
        from app.db.models import Message

        async with self._session_factory() as session:
            session.add(
                Message(
                    chat_id=chat_id,
                    direction=Direction.outgoing,
                    author=author,
                    text=text,
                    status=status,
                    llm_meta=llm_meta,
                )
            )
            await session.commit()

    async def load_history(self, chat_id: str, limit: int = HISTORY_LIMIT) -> list[dict]:
        from sqlalchemy import select

        from app.db.models import Message

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(Message)
                    .where(
                        Message.chat_id == chat_id,
                        Message.status.not_in(HISTORY_EXCLUDED_STATUSES),
                    )
                    # Последние N по времени, потом разворот: ORDER BY ... DESC
                    # LIMIT N — единственный способ взять «хвост» диалога, не
                    # вычитывая его целиком.
                    .order_by(Message.created_at.desc(), Message.id.desc())
                    .limit(limit)
                )
            ).scalars().all()

        return [
            {
                "role": "user" if row.direction == Direction.incoming else "assistant",
                "content": row.text or "",
            }
            for row in reversed(rows)
            if (row.text or "").strip()
        ]

    async def load_dialog_state(
        self, chat_id: str
    ) -> tuple[DialogConcessionState, TouchState]:
        from sqlalchemy import select

        from app.db.models import DialogState

        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(DialogState).where(DialogState.chat_id == chat_id)
                )
            ).scalar_one_or_none()

        if row is None:
            return DialogConcessionState(), TouchState()

        return (
            row.to_runtime(),
            TouchState(
                touch_count=row.touch_count,
                last_touch_at=row.last_touch_at,
                next_touch_due_at=row.next_touch_due_at,
            ),
        )

    async def save_dialog_state(
        self, chat_id: str, concession: DialogConcessionState, touch: TouchState
    ) -> None:
        from sqlalchemy import select

        from app.db.models import DialogState

        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(DialogState).where(DialogState.chat_id == chat_id)
                )
            ).scalar_one_or_none()
            if row is None:
                row = DialogState(chat_id=chat_id)
                session.add(row)

            row.base_price_quoted = concession.base_price_quoted
            row.used_tiers = sorted(concession.used_tiers)
            row.floor_reached = concession.floor_reached
            row.concessions_count = len(concession.used_tiers)
            row.touch_count = touch.touch_count
            row.last_touch_at = touch.last_touch_at
            row.next_touch_due_at = touch.next_touch_due_at
            await session.commit()

    async def bump_agent_reply_count(self, chat_id: str) -> None:
        from sqlalchemy import select

        from app.db.models import Chat

        async with self._session_factory() as session:
            chat = (
                await session.execute(select(Chat).where(Chat.chat_id == chat_id))
            ).scalar_one_or_none()
            if chat is None:
                return
            chat.agent_reply_count = (chat.agent_reply_count or 0) + 1
            await session.commit()

    async def get_chat_item_id(self, chat_id: str) -> Optional[str]:
        """Объявление чата — для `OutboundGate` перед отправкой клиенту.

        Отдельный SELECT, а не `get_or_create_chat`: проверка права писать
        не должна заводить строку в базе для чата, которого мы, возможно,
        и знать не хотим.
        """
        from sqlalchemy import select

        from app.db.models import Chat

        async with self._session_factory() as session:
            return (
                await session.execute(
                    select(Chat.item_id).where(Chat.chat_id == chat_id)
                )
            ).scalar_one_or_none()

    async def set_chat_manual_hold(self, chat_id: str, value: bool) -> bool:
        """Ручной hold — тот самый флаг для 65 чатов инцидента 2026-08-28.

        Ставится и снимается ТОЛЬКО отсюда (команды /hold и /unhold): его не
        трогают ни кулдаун, ни возврат чата агенту, ни режим перехвата.
        Смысл флага в том и есть, что снимает его человек.
        """
        from sqlalchemy import select

        from app.db.models import Chat

        async with self._session_factory() as session:
            chat = (
                await session.execute(select(Chat).where(Chat.chat_id == chat_id))
            ).scalar_one_or_none()
            if chat is None:
                # Держать можно и чат, которого мы ещё не видели: оператор
                # ставит hold по инциденту, а не по нашей готовности.
                chat = Chat(chat_id=chat_id)
                session.add(chat)
            chat.manual_hold = value
            await session.commit()
        return value

    async def was_sent_by_us(self, chat_id: str, text: str) -> bool:
        """Это эхо НАШЕГО ЖЕ сообщения, а не текст живого менеджера?

        Авито присылает событие на каждое исходящее — и на наши, и на
        написанные менеджером руками из интерфейса Авито. У всех у них
        `author_id` равен нашему аккаунту (тот самый признак, по которому
        отсекается эхо), поэтому отличить одно от другого можно только по
        тому, писали мы этот текст сами или нет: свои отправки лежат в
        `messages`, чужих там нет.

        Ошибиться можно в две стороны, и они неравноценны:
          * приняли менеджера за себя — кулдаун не включится, агент продолжит
            отвечать поверх человека (это и есть исходный инцидент);
          * приняли себя за менеджера — агент замолчит на окно кулдауна после
            КАЖДОГО своего ответа, то есть замолчит совсем.
        Второе несравнимо хуже, поэтому сравнение точное: свой текст мы знаем
        дословно и совпадём с ним наверняка, а менеджер совпадёт, только если
        дословно повторит недавний ответ агента.
        """
        from sqlalchemy import select

        from app.db.models import Message

        normalized = (text or "").strip()
        if not normalized:
            # Картинка или системное событие: текста нет, сравнивать нечего,
            # и «нашим» это считать нельзя — агент картинок не шлёт.
            return False

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(Message.text)
                    .where(
                        Message.chat_id == chat_id,
                        Message.direction == Direction.outgoing,
                        Message.author == Author.agent,
                    )
                    .order_by(Message.id.desc())
                    .limit(ECHO_LOOKBACK)
                )
            ).scalars().all()

        return any((row or "").strip() == normalized for row in rows)

    async def last_incoming_at(self, chat_id: str) -> Optional[datetime]:
        """Когда клиент писал в этот чат в последний раз.

        Нужен путям, которые отправляют НЕ в ответ на входящее, а по
        сохранённому состоянию: отложенные касания и запасной ответ по
        таймауту уступки. У них нет payload'а с `created`, поэтому возраст
        переписки берётся из истории. None — клиент не писал ни разу (или
        чата нет), и для порога это значит «не свежее».
        """
        from sqlalchemy import select

        from app.db.models import Message

        async with self._session_factory() as session:
            return (
                await session.execute(
                    select(Message.created_at)
                    .where(
                        Message.chat_id == chat_id,
                        Message.direction == Direction.incoming,
                    )
                    .order_by(Message.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

    async def get_chat_manual_hold(self, chat_id: str) -> bool:
        """Ручной hold — для `OutboundGate` перед отправкой клиенту.

        Отдельный SELECT по той же причине, что и у `get_chat_item_id`:
        проверка права писать не должна заводить строку в базе. Отсутствие
        чата означает «hold не ставили» — False, а не отказ в отправке.
        """
        from sqlalchemy import select

        from app.db.models import Chat

        async with self._session_factory() as session:
            value = (
                await session.execute(
                    select(Chat.manual_hold).where(Chat.chat_id == chat_id)
                )
            ).scalar_one_or_none()
            return bool(value)

    async def get(self, item_id: str) -> Optional[ItemZoneRow]:
        """Реализация `ItemZoneLookup` — какому объявлению какая зона
        соответствует (app/agent/listing_context.py). Живёт здесь, а не
        отдельным классом: это тот же самый доступ к тем же таблицам, и
        конвейеру удобнее передать в `AgentLoop` один объект."""
        from sqlalchemy import select

        from app.db.models import ItemZoneMap

        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(ItemZoneMap).where(ItemZoneMap.item_id == item_id)
                )
            ).scalar_one_or_none()

        if row is None:
            return None
        return ItemZoneRow(zone_id=row.zone_id, category=row.category)

    async def log_concession(self, chat_id: str, event: ConcessionEvent) -> None:
        """Пишет только то, что дошло до фильтра
        `ConcessionEvent.needs_operator_approval` (вызывающий код —
        app/pipeline.py решает это ДО вызова) — обычные R1/R2/R6 отказы,
        которых большинство ходов, в этой таблице только шумели бы: она
        для аудита реальных ценовых решений и для «сколько уступок выдано
        сегодня» на карточке оператора, а не зеркало логов decide()
        (те уже есть в logger.info, см. app.pricing.concessions._log)."""
        from app.db.models import ConcessionLog

        decision = event.decision
        async with self._session_factory() as session:
            session.add(
                ConcessionLog(
                    dialog_id=chat_id,
                    zone=event.zone_id,
                    tier=decision.tier,
                    trigger=event.trigger,
                    base_price=event.base_price,
                    final_price=decision.new_quote.total if decision.new_quote else None,
                    revenue_delta=decision.revenue_delta,
                    revenue_delta_basis=decision.revenue_delta_basis,
                    exchange_given=decision.exchange_required or None,
                    allowed=decision.allowed,
                    denial_reason=decision.denial_reason,
                    provisional_policy=decision.provisional_policy,
                )
            )
            await session.commit()

    async def count_concessions_today(self, now: Optional[datetime] = None) -> int:
        """Только реально ВЫДАННЫЕ (allowed=True) — «требует одобрения, но
        ещё не решено» и «оператор отклонил» в это число не входят. Точность
        ограничена: строка пишется в момент, когда ход дошёл до модерации, а
        не в момент фактического одобрения (см. app/pipeline.py) — значит
        отклонённая в течение дня уступка на короткое время всё же
        засчитывается в «сегодня выдано». Известный компромисс, не бага: для
        карточки оператора нужна оценка, а не бухгалтерская точность.

        Границу дня считаем по Москве (см. MOSCOW_TZ) — `created_at`
        хранится как `timestamptz`, и сравнение с MSK-датой корректно
        независимо от того, в каком часовом поясе Postgres хранит значение
        внутри: сравнение timestamptz идёт по фактическому моменту, не по
        текстовому представлению. `now` — реальное текущее время по
        умолчанию; параметризовано ради теста границы дня, который иначе
        зависел бы от того, в какой час (по UTC) реально идёт прогон —
        разница между MSK- и UTC-полночью проявляется только в окне
        21:00–00:00 UTC, и тест на случайное время суток её просто не видит.
        """
        from sqlalchemy import func, select

        from app.db.models import ConcessionLog

        now_msk = (now or datetime.now(MOSCOW_TZ)).astimezone(MOSCOW_TZ)
        today_start_msk = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)
        async with self._session_factory() as session:
            count = (
                await session.execute(
                    select(func.count()).select_from(ConcessionLog).where(
                        ConcessionLog.allowed.is_(True),
                        ConcessionLog.created_at >= today_start_msk,
                    )
                )
            ).scalar()
        return count or 0


class SqlAlchemyBookingSink:
    """Пишет поставленную агентом бронь в нашу таблицу `bookings`.

    Отдельный маленький класс, а не метод `SqlAlchemyDialogStore`: бронь —
    не часть переписки, и исполнителю инструментов (`ToolExecutor`) нужен
    именно узкий объект с одним `save`, а не весь стор диалога.

    Ошибку НЕ глушит: вызывающий код (`_tool_create_booking`) сам решает,
    что делать со сбоем записи — там бронь уже стоит в YCLIENTS, и падать
    из-за нашей таблицы нельзя, но и делать вид, что записалось, тоже.
    """

    def __init__(self, session_factory):
        self._session_factory = session_factory

    async def save(self, **record) -> None:
        from app.db.models import Booking

        async with self._session_factory() as session:
            session.add(Booking(**record))
            await session.commit()
