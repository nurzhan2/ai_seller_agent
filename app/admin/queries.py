"""Источники данных для админки — чтение из той же БД, куда пишет конвейер.

Почему это появилось отдельным файлом. Страницы `/admin/dialogs`,
`/admin/leads`, `/admin/concessions` и `/admin/costs` были написаны против
интерфейсов (`request.app.state.dialog_provider` и т.д.), у которых НЕ БЫЛО
НИ ОДНОЙ РЕАЛИЗАЦИИ — только фейки в тестах. Пока конвейера не существовало,
это было честно: показывать нечего, страница так и писала «источник не
подключён». Как только конвейер начал писать `Chat`/`Message` в базу,
надпись превратилась в ложь — данные есть, просто их некому прочитать.

Только чтение. Ни один метод здесь ничего не меняет: админка — смотровое
окно, а не второй путь записи рядом с конвейером.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

logger = logging.getLogger("parmangal.admin.queries")

# Сколько строк отдаём на страницу. Админка — обзор последнего, а не выгрузка
# всей истории: без потолка первая же сотня диалогов сделала бы страницу
# неоткрываемой. Для полной выгрузки лидов есть /admin/leads.csv.
PAGE_LIMIT = 200

# За какой период считается расход на модели. Совпадает с горизонтом, на
# котором вообще осмысленно смотреть дневной лимит из настроек.
COST_WINDOW_DAYS = 30


def _to_decimal(value: Any) -> Decimal:
    """llm_meta.cost_rub лежит строкой (Decimal не сериализуется в JSON).
    Битое/пустое значение — это ноль, а не падение страницы расходов."""
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


class SqlAlchemyAdminQueries:
    """Все четыре провайдера админки в одном объекте.

    Один объект, а не четыре: страницы читают одну и ту же базу через одну и
    ту же фабрику сессий, и дробить это на четыре класса с идентичным
    конструктором — лишняя церемония. В `app.state` он кладётся под всеми
    четырьмя именами, которых ждут маршруты.
    """

    def __init__(self, session_factory):
        self._session_factory = session_factory

    # -- /admin/dialogs ----------------------------------------------------

    async def list_dialogs(self, limit: int = PAGE_LIMIT) -> list[dict]:
        from sqlalchemy import func, select

        from app.db.models import Chat, Message

        async with self._session_factory() as session:
            # Счётчик сообщений подзапросом, а не отдельным запросом на каждый
            # чат: на сотне диалогов это разница между одним обращением к базе
            # и сотней одного и того же вида (классический N+1).
            counts = (
                select(Message.chat_id, func.count().label("n"))
                .group_by(Message.chat_id)
                .subquery()
            )
            rows = (
                await session.execute(
                    select(Chat, counts.c.n)
                    .outerjoin(counts, counts.c.chat_id == Chat.chat_id)
                    .order_by(Chat.last_msg_at.desc().nullslast(), Chat.id.desc())
                    .limit(limit)
                )
            ).all()

        return [
            {
                "chat_id": chat.chat_id,
                "zone_id": chat.zone_id,
                "item_id": chat.item_id,
                "buyer_name": chat.buyer_name,
                "state": chat.state.value if chat.state is not None else None,
                "is_human_takeover": chat.is_human_takeover,
                "ai_enabled": chat.ai_enabled,
                "messages": n or 0,
                "last_msg_at": chat.last_msg_at,
            }
            for chat, n in rows
        ]

    # -- /admin/leads ------------------------------------------------------

    async def list_leads(self, limit: int = PAGE_LIMIT) -> list[dict]:
        from sqlalchemy import select

        from app.db.models import Lead

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(Lead).order_by(Lead.created_at.desc(), Lead.id.desc()).limit(limit)
                )
            ).scalars().all()

        return [
            {
                "chat_id": row.chat_id,
                "name": row.name,
                "phone": row.phone,
                "zone_id": row.zone_id,
                "date": row.date,
                "guests": row.guests,
                "notes": row.notes,
                "created_at": row.created_at,
            }
            for row in rows
        ]

    # -- /admin/concessions ------------------------------------------------

    async def list_concessions(self, limit: int = PAGE_LIMIT) -> list[dict]:
        from sqlalchemy import select

        from app.db.models import ConcessionLog

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(ConcessionLog)
                    # Только выданные: страница называется «журнал уступок» и
                    # считает недополученную выручку. Отказы там же исказили бы
                    # сумму — они пишутся в лог по правилу R12 ради разбора
                    # «почему не дали», а не ради денег.
                    .where(ConcessionLog.allowed.is_(True))
                    .order_by(ConcessionLog.created_at.desc(), ConcessionLog.id.desc())
                    .limit(limit)
                )
            ).scalars().all()

        return [
            {
                "dialog_id": row.dialog_id,
                "zone": row.zone,
                "tier": row.tier,
                "trigger": row.trigger,
                "base_price": row.base_price,
                "final_price": row.final_price,
                "revenue_delta": row.revenue_delta,
                "revenue_delta_basis": row.revenue_delta_basis,
                "exchange_given": row.exchange_given,
                "provisional_policy": row.provisional_policy,
                "created_at": row.created_at,
            }
            for row in rows
        ]

    # -- /admin/costs ------------------------------------------------------

    async def list_costs(self, days: int = COST_WINDOW_DAYS) -> list[dict]:
        """Расход на модели по дням × провайдер × модель.

        Агрегация в Python, а не в SQL, намеренно: числа лежат внутри JSONB
        (`Message.llm_meta`), причём `cost_rub` — строкой. Складывать их в
        SQL значит писать приведение вида `(llm_meta->>'cost_rub')::numeric`,
        которое падает на первой же строке с чем-то неожиданным внутри и
        роняет всю страницу. Объём за 30 дней — это сообщения одного
        небольшого аккаунта Авито, такое спокойно складывается в процессе.
        """
        from sqlalchemy import select

        from app.db.models import Direction, Message

        since = datetime.now(timezone.utc) - timedelta(days=days)

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(Message.chat_id, Message.llm_meta, Message.created_at)
                    .where(
                        Message.direction == Direction.outgoing,
                        Message.llm_meta.is_not(None),
                        Message.created_at >= since,
                    )
                )
            ).all()

        # (дата, провайдер, модель) -> {'cost': Decimal, 'chats': set}
        buckets: dict[tuple, dict] = {}
        for chat_id, meta, created_at in rows:
            if not isinstance(meta, dict):
                continue
            key = (
                created_at.date() if created_at is not None else None,
                meta.get("provider") or "—",
                meta.get("model") or "—",
            )
            bucket = buckets.setdefault(key, {"cost": Decimal("0"), "chats": set()})
            bucket["cost"] += _to_decimal(meta.get("cost_rub"))
            bucket["chats"].add(chat_id)

        result = []
        for (day, provider, model), bucket in sorted(
            buckets.items(), key=lambda kv: (kv[0][0] is None, kv[0][0]), reverse=True
        ):
            dialogs = len(bucket["chats"])
            cost = bucket["cost"].quantize(Decimal("0.01"))
            result.append(
                {
                    "date": day,
                    "llm_provider": provider,
                    "model": model,
                    "dialogs": dialogs,
                    "cost_rub": cost,
                    "cost_per_dialog": (
                        (cost / dialogs).quantize(Decimal("0.01")) if dialogs else Decimal("0")
                    ),
                }
            )
        return result

    # -- /stats в Telegram --------------------------------------------------

    async def stats(self, ops_store: Optional[Any] = None) -> dict:
        """Данные для `render_stats` — команда /stats операторского бота.

        `ops_store` нужен ради счётчиков модерации: у БД-реализации они
        восстанавливаются из журнала действий, у in-memory лежат в словаре.
        Без него отдаём нули, а не падаем — /stats остаётся доступным даже
        если стор почему-то не передали.
        """
        from sqlalchemy import func, select

        from app.db.models import Chat, ConcessionLog, Lead

        async with self._session_factory() as session:
            dialogs = (await session.execute(select(func.count()).select_from(Chat))).scalar() or 0
            leads = (await session.execute(select(func.count()).select_from(Lead))).scalar() or 0
            escalations = (
                await session.execute(
                    select(func.count()).select_from(Chat).where(Chat.state == "escalated")
                )
            ).scalar() or 0

        costs = await self.list_costs()
        cost_total = sum((row["cost_rub"] for row in costs), Decimal("0"))

        moderation = {"approved": 0, "edited": 0, "rejected": 0}
        if ops_store is not None:
            getter = getattr(ops_store, "moderation_stats", None)
            if getter is not None:
                moderation = await getter()
            else:
                moderation = dict(getattr(ops_store, "moderation", moderation))

        return {
            "dialogs": dialogs,
            "leads": leads,
            "escalations": escalations,
            "cost_rub": cost_total,
            "approved": moderation.get("approved", 0),
            "edited": moderation.get("edited", 0),
            "rejected": moderation.get("rejected", 0),
        }
