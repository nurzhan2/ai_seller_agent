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
