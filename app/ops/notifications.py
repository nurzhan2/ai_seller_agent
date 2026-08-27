"""Сборка сообщений оператору.

Отделено от обработчиков намеренно: текст и клавиатуры — чистые функции без
сети и без aiogram-контекста, поэтому их можно проверять тестами построчно.
Именно здесь решается, что оператор увидит про каждую скидку и каждую
эскалацию, а это в проекте важнее, чем механика кнопок.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

AVITO_CHAT_URL = "https://www.avito.ru/profile/messenger/channel/{chat_id}"

MAX_TELEGRAM_LEN = 4096


@dataclass(frozen=True)
class DialogCard:
    chat_id: str
    item_title: Optional[str]
    buyer_name: Optional[str]
    client_text: str
    agent_text: str
    dry_run: bool
    escalated: bool = False
    escalation_reason: Optional[str] = None


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_dialog_card(card: DialogCard) -> str:
    lines = [
        f"🆕 Авито · {card.item_title or 'объявление не определено'}",
        f"👤 {card.buyer_name or 'без имени'}",
        "",
        f"💬 {_clip(card.client_text, 900)}",
        "",
        f"🤖 {_clip(card.agent_text, 1500) or '(агент промолчал)'}",
    ]
    if card.dry_run:
        lines.append("")
        lines.append("⚠️ НЕ ОТПРАВЛЕНО — режим модерации, нужна ваша кнопка")
    if card.escalated:
        lines.append("")
        lines.append(f"🔴 Эскалация: {card.escalation_reason or 'причина не указана'}")
    return _clip("\n".join(lines), MAX_TELEGRAM_LEN)


def dialog_keyboard(chat_id: str, dry_run: bool, taken_over: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    if dry_run and not taken_over:
        rows.append(
            [InlineKeyboardButton(text="✅ Одобрить и отправить", callback_data=f"approve:{chat_id}")]
        )
        rows.append(
            [InlineKeyboardButton(text="🚫 Отклонить", callback_data=f"reject:{chat_id}")]
        )

    if taken_over:
        rows.append(
            [InlineKeyboardButton(text="↩️ Вернуть ИИ", callback_data=f"return_ai:{chat_id}")]
        )
    else:
        rows.append(
            [InlineKeyboardButton(text="🙋 Взять на себя", callback_data=f"takeover:{chat_id}")]
        )

    rows.append(
        [InlineKeyboardButton(text="🔗 Открыть в Авито", url=AVITO_CHAT_URL.format(chat_id=chat_id))]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dataclass(frozen=True)
class ConcessionRequestCard:
    """Всё, что нужно оператору, чтобы решить по ценовой уступке НЕ
    заходя в админку — ровно то, что просили: текст клиента и черновик
    Иришки, ступень и триггер, база → скидка → недополучено, сколько уже
    выдано сегодня, пометка provisional. `final_price`/`revenue_delta`
    приходят `None`, когда решение — requires_operator_approval (загрузка
    неизвестна): движок его ещё не посчитал, это ЕГО и есть вопрос
    оператору, а не сокрытая цифра."""

    chat_id: str
    client_text: str
    agent_text: str
    tier: Optional[int]
    trigger: Optional[str]
    reason: str                       # почему нужен человек — denial_reason
    base_price: Optional[Decimal]
    final_price: Optional[Decimal]
    revenue_delta: Optional[Decimal]
    concessions_today: int
    provisional: bool


def render_concession_request(card: ConcessionRequestCard) -> str:
    lines = [
        f"💸 ЗАПРОС НА СКИДКУ · чат {card.chat_id}",
        "",
        f"💬 Клиент: {_clip(card.client_text, 700)}",
        f"🤖 Иришка ответит: {_clip(card.agent_text, 1200) or '(пусто)'}",
        "",
        f"Ступень: {card.tier if card.tier is not None else '—'}"
        f" · триггер: {card.trigger or '—'}",
    ]
    if card.base_price is not None and card.final_price is not None:
        lines.append(
            f"Цена: {card.base_price} ₽ → {card.final_price} ₽"
            f" (недополучаем {abs(card.revenue_delta or Decimal('0'))} ₽)"
        )
    else:
        lines.append(f"Цена: пока не посчитана — {card.reason}")
    lines.append(f"Уступок сегодня уже выдано: {card.concessions_today}")
    if card.provisional:
        lines.append("⚠️ Правило предварительное (наше, не подтверждённое заказчиком) — вопрос 13.4")
    lines.append("")
    lines.append("Нужно решение: разрешить, отклонить или взять чат на себя.")
    return _clip("\n".join(lines), MAX_TELEGRAM_LEN)


def concession_keyboard(chat_id: str) -> InlineKeyboardMarkup:
    """Те же действия и callback_data, что у `dialog_keyboard` (approve/
    reject/takeover — хендлеры в app/ops/handlers.py их не различают по
    источнику), только подписи говорят именно про скидку, а не про
    сообщение вообще."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Разрешить скидку", callback_data=f"approve:{chat_id}")],
            [InlineKeyboardButton(text="🚫 Отклонить", callback_data=f"reject:{chat_id}")],
            [InlineKeyboardButton(text="🙋 Взять на себя", callback_data=f"takeover:{chat_id}")],
            [InlineKeyboardButton(text="🔗 Открыть в Авито", url=AVITO_CHAT_URL.format(chat_id=chat_id))],
        ]
    )


def render_daily_limit_notice(chat_id: str, limit: int) -> str:
    """R10: дневной лимит уступок исчерпан. НЕ эскалация — агент продолжает
    диалог сам, просто без скидки; сообщение только чтобы оператор знал,
    что с этого момента клиенты дня идут без уступок, и мог вмешаться
    вручную, если считает нужным (следующий такой чат — та же карточка)."""
    return "\n".join(
        [
            f"⚠️ ДНЕВНОЙ ЛИМИТ УСТУПОК ИСЧЕРПАН ({limit}) · чат {chat_id}",
            "",
            "Клиенту в этом чате скидка не предложена — агент продолжает",
            "разговор без неё. Остальные клиенты сегодня — тоже без скидки,",
            "пока лимит не сбросится (в полночь по Москве).",
        ]
    )


def render_escalation(chat_id: str, reason: str, urgency: str = "normal") -> str:
    mark = {"high": "🔴🔴", "normal": "🔴", "low": "🟠"}.get(urgency, "🔴")
    return "\n".join(
        [
            f"{mark} ЭСКАЛАЦИЯ · чат {chat_id}",
            "",
            f"Причина: {reason}",
            "",
            "Агент остановлен по этому чату и ждёт вас.",
        ]
    )


def render_concession(
    chat_id: str,
    tier: Optional[int],
    kind: Optional[str],
    trigger: Optional[str],
    revenue_delta: Decimal,
    provisional: bool,
    offer_template: str,
) -> str:
    """Оператор обязан видеть КАЖДУЮ скидку.

    Отдельным сообщением, а не строкой в карточке диалога: скидка — это
    расход, и он не должен теряться среди обычной переписки.
    """
    lines = [
        f"💸 УСТУПКА · чат {chat_id}",
        f"Ступень {tier} ({'ценовая' if kind == 'price' else 'неценовая'})",
        f"Триггер: {trigger or 'не указан'}",
        f"Недополучено: {revenue_delta} ₽",
    ]
    if provisional:
        lines.append(
            "⚠️ Правило предварительное (наше, не подтверждённое заказчиком) — вопрос 13.4"
        )
    lines.append("")
    lines.append(f"Текст клиенту: {_clip(offer_template, 600)}")
    return _clip("\n".join(lines), MAX_TELEGRAM_LEN)


def render_stats(
    dialogs: int,
    leads: int,
    escalations: int,
    cost_rub: Decimal,
    approved: int,
    edited: int,
    rejected: int,
) -> str:
    total_moderated = approved + edited + rejected
    share = f"{approved / total_moderated:.0%}" if total_moderated else "—"
    return "\n".join(
        [
            "📊 Статистика",
            "",
            f"Диалогов: {dialogs}",
            f"Лидов: {leads}",
            f"Эскалаций: {escalations}",
            f"Расход на модели: {cost_rub} ₽",
            "",
            "Модерация:",
            f"  одобрено без правок: {approved}",
            f"  исправлено: {edited}",
            f"  отклонено: {rejected}",
            f"  доля чистых одобрений: {share}",
            "",
            "Порог выключения модерации — 90% три дня подряд.",
        ]
    )


def render_digest(
    dialogs: int,
    leads: list[dict],
    escalations: list[str],
    concessions_total: Decimal,
    concessions_count: int,
    unanswered_topics: list[str],
) -> str:
    lines = ["🌙 Дайджест за день", "", f"Диалогов: {dialogs}", f"Лидов: {len(leads)}"]

    if leads:
        lines.append("")
        lines.append("Контакты:")
        for lead in leads[:20]:
            zone = lead.get("zone_id") or "зона не указана"
            lines.append(
                f"  • {lead.get('name') or 'без имени'} · {lead.get('phone')} · {zone}"
            )

    lines.append("")
    lines.append(f"Эскалаций: {len(escalations)}")
    for reason in escalations[:10]:
        lines.append(f"  • {reason}")

    lines.append("")
    lines.append(f"Уступок: {concessions_count} на сумму {concessions_total} ₽")

    if unanswered_topics:
        lines.append("")
        lines.append("Вопросы, на которые у нас нет ответа:")
        for topic in unanswered_topics[:10]:
            lines.append(f"  • {topic}")

    return _clip("\n".join(lines), MAX_TELEGRAM_LEN)


def render_booking_notice(record: dict) -> str:
    """Факт поставленной агентом брони. БЕЗ КНОПОК намеренно.

    Бронь уже стоит в YCLIENTS — одобрять нечего, и кнопка «Разрешить»
    рядом с уже случившимся фактом только путала бы. Оператору здесь нужно
    ровно одно: знать, что произошло, и иметь под рукой всё, чтобы
    вмешаться вручную, если что-то не так.

    Часы показаны обоими числами, когда они расходятся: при акции «6-й час
    в подарок» гость занимает 6 часов, платит за 5, и оператор должен
    видеть, что заблокировано именно 6 — иначе шестой час выглядит как
    ошибка агента.
    """
    occupied = record.get("occupied_hours")
    billable = record.get("billable_hours")
    if billable is not None and occupied is not None and billable != occupied:
        hours_line = f"Часы: занято {occupied}, оплачено {billable}"
        promo = record.get("applied_promo")
        if promo:
            hours_line += f" (акция: {promo})"
    else:
        hours_line = f"Часы: {occupied}"

    lines = [
        f"📅 БРОНЬ ПОСТАВЛЕНА АГЕНТОМ · чат {record.get('chat_id')}",
        "",
        f"Зона: {record.get('zone_id')}",
        f"Дата: {record.get('booking_date')} в {record.get('start_time')}",
        hours_line,
    ]
    if record.get("guests"):
        lines.append(f"Гостей: {record['guests']}")
    if record.get("total") is not None:
        lines.append(f"Сумма: {record['total']} ₽")
    lines.append(f"Клиент: {record.get('client_name') or '—'}, {record.get('client_phone') or '—'}")
    lines.append(f"ID записи в YCLIENTS: {record.get('record_id') or '—'}")
    lines.append("")
    lines.append("Подтверждать не нужно — бронь уже в YCLIENTS. Это уведомление.")
    return "\n".join(lines)
