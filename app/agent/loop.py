"""Диалоговый цикл агента.

Порядок работы одного хода:
    1. дешёвая классификация входящего (модель-классификатор провайдера) —
       спам, оффтоп, просьба позвать человека отсекаются до основного вызова;
    2. основной вызов модели-диалога с инструментами, до MAX_TOOL_ITERATIONS
       витков;
    3. сбор текста ответа и метаданных по токенам и стоимости.

Ограничение витков — не оптимизация, а предохранитель: модель, зациклившаяся
на инструментах, должна упереться в потолок и уйти к человеку, а не жечь
бюджет молча.

Провайдер (Anthropic или DeepSeek — промт №12) не влияет ни на что здесь:
`self.provider` — это `LLMProvider`, и цикл не знает и не должен знать,
кто именно отвечает на `.complete()`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional, Sequence

from app.agent.listing_context import (
    ItemZoneLookup,
    build_listing_hint,
    no_listing_hint,
    resolve_listing,
)
from app.agent.prompts import CLASSIFIER_PROMPT, build_system_prompt
from app.agent.providers.anthropic_provider import PRICE_PER_MTOK_RUB as _ANTHROPIC_RATES
from app.agent.providers.anthropic_provider import AnthropicProvider
from app.agent.providers.base import LLMProvider
from app.agent.providers.deepseek_provider import PRICE_PER_MTOK_RUB as _DEEPSEEK_RATES
from app.agent.tools import TOOLS, ToolExecutor, tool_result_block
from app.kb.loader import KnowledgeBase

logger = logging.getLogger("parmangal.loop")

MAIN_MODEL = "claude-sonnet-5"
CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"

MAX_TOOL_ITERATIONS = 5
MAX_TOKENS = 1024
HISTORY_WINDOW = 30
MAX_AGENT_REPLIES_PER_CHAT = 25

# Цены за миллион токенов, рубли. Один источник для /admin/costs и для
# оценки стоимости хода — объединяет таблицы обоих провайдеров, чтобы
# estimate_cost_rub работала независимо от того, кто на самом деле обслужил
# ход (промт №12).
PRICE_PER_MTOK_RUB: dict[str, tuple[Decimal, Decimal]] = {
    **_ANTHROPIC_RATES,
    **_DEEPSEEK_RATES,
}

# Последний рубеж (промт №12, Часть 2): если за ход не было ни одного вызова
# инструмента, а в тексте всё равно всплыла цифра рядом с рублями — это
# нарушение главного инварианта проекта (цена только из calculate_price), и
# неважно, какой провайдер её сочинил. Работает всегда, а не только когда
# подведёт что-то более умное на стороне модели.
#
# Тот же паттерн, что и в app.quality.asserts._MONEY (харнесс импортирует
# отсюда, а не наоборот) — раньше здесь была своя, более узкая копия
# (`\d\s*(?:₽|руб)`), и живой прогон на DeepSeek поймал ровно то, чего она
# не ловила: «10 500р.» (пробел между разрядами, сокращение «р.» без точки
# после «руб») харнесс засчитал как «цена без инструмента», а этот рубеж —
# нет, потому что пропустил мимо. Два разных regex для одного и того же
# инварианта расходятся молча; один источник истины этого не допускает.
PRICE_LIKE_WITHOUT_TOOL_CALL = re.compile(
    r"\b\d[\d\s]{1,9}\d\s*(?:₽|руб|р\.)|\b\d{3,6}\s*(?:₽|руб)", re.IGNORECASE
)
GUARD_RAIL_VIOLATION = "цена в тексте без вызова инструмента (последний рубеж)"


@dataclass
class TurnResult:
    text: str
    escalated: bool = False
    escalation_reason: Optional[str] = None
    classification: Optional[str] = None
    tool_calls: list[str] = field(default_factory=list)
    quote_statuses: list[str] = field(default_factory=list)
    granted_offer_templates: list[str] = field(default_factory=list)
    llm_meta: dict[str, Any] = field(default_factory=dict)
    hit_iteration_limit: bool = False
    # Сколько раз за ход вызов инструмента вернул {"error": ...} — модель
    # прислала аргументы, не прошедшие валидацию, и получила это обратно
    # вместо результата. Метрика для сравнения провайдеров (промт №12,
    # Часть 4: «доля ходов, где инструмент вызван корректно с первой попытки»).
    tool_call_errors: int = 0
    # `DialogConcessionState` ПОСЛЕ хода — то, что накопил ToolExecutor:
    # floor_reached (храповик), used_tiers, base_price_quoted, touch_count.
    # Без этого поля конвейер (app/pipeline.py) не может сохранить храповик
    # в БД, а несохранённый храповик — это ровно та утечка, ради которой
    # существует app/pricing/quote_gate.py: после перезапуска процесса агент
    # назовёт цену ВЫШЕ уже обещанной. None означает «ход не дошёл до
    # исполнителя инструментов» (спам, просьба позвать человека) — состояние
    # не изменилось, перезаписывать в БД нечего.
    concession_state: Any = None
    # Каждое решение decide() за ход — не только выданные, и не только
    # ценовые. Конвейер (app/pipeline.py) фильтрует их сам через
    # ConcessionEvent.needs_operator_approval, решая, нужно ли одобрение.
    concession_events: list[Any] = field(default_factory=list)


def estimate_cost_rub(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    rates = PRICE_PER_MTOK_RUB.get(model)
    if rates is None:
        return Decimal("0")
    per_in, per_out = rates
    million = Decimal("1000000")
    cost = (Decimal(input_tokens) / million) * per_in + (
        Decimal(output_tokens) / million
    ) * per_out
    return cost.quantize(Decimal("0.01"))


def _text_from_blocks(blocks: Sequence[Any]) -> str:
    parts: list[str] = []
    for block in blocks:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(p.strip() for p in parts if p.strip()).strip()


class AgentLoop:
    def __init__(
        self,
        client: Any,                       # LLMProvider, anthropic.AsyncAnthropic или мок
        kb: KnowledgeBase,
        executor_factory: Any = None,
        dialog_model: str = MAIN_MODEL,
        classifier_model: str = CLASSIFIER_MODEL,
        booking_provider: Any = None,       # BookingProvider — YClientsProvider или None
        concessions_today_provider: Any = None,   # async () -> int, R10 дневной лимит
        booking_sink: Any = None,           # .save(**record) — запись брони в нашу БД
        booking_notifier: Any = None,       # async (record) -> None — уведомление оператору
    ):
        # `client` принимает три формы ради обратной совместимости с уже
        # написанными тестами и харнессом: готовый LLMProvider, «сырой»
        # anthropic-подобный клиент (заворачивается в AnthropicProvider без
        # изменения поведения) или мок с тем же интерфейсом `.messages.create`.
        self.provider: LLMProvider = (
            client if isinstance(client, LLMProvider) else AnthropicProvider(client=client)
        )
        self.kb = kb
        self.dialog_model = dialog_model
        self.classifier_model = classifier_model
        # `self.kb`, а НЕ захваченный параметр `kb`: база знаний
        # перезагружается на лету, когда оператор правит цену из Telegram
        # (app/ops/menu_service.py), и обновление `agent_loop.kb` обязано
        # доезжать до новых исполнителей инструментов. С захваченным
        # параметром правка молча не влияла бы на расчёт до рестарта.
        self.executor_factory = executor_factory or (
            lambda dialog_id, state, concessions_blocked=False: ToolExecutor(
                self.kb, dialog_id, state, booking_provider=booking_provider,
                concessions_blocked=concessions_blocked,
                concessions_today_provider=concessions_today_provider,
                booking_sink=booking_sink,
                booking_notifier=booking_notifier,
            )
        )

    # -- классификация -----------------------------------------------------

    async def classify(self, text: str) -> str:
        response = await self.provider.complete(
            model=self.classifier_model,
            max_tokens=8,
            system=CLASSIFIER_PROMPT,
            messages=[{"role": "user", "content": text}],
        )
        label = _text_from_blocks(response.content).strip().lower().split()
        return label[0] if label else "question"

    # -- основной ход ------------------------------------------------------

    async def run_turn(
        self,
        dialog_id: str,
        history: list[dict],
        user_text: str,
        state: Any = None,
        item_id: Optional[str] = None,
        item_lookup: Optional[ItemZoneLookup] = None,
        concessions_blocked: bool = False,
    ) -> TurnResult:
        classification = await self.classify(user_text)

        if classification == "human":
            # Просьба позвать человека не обсуждается и не «отрабатывается».
            return TurnResult(
                text="Конечно, сейчас передам менеджеру — он свяжется с вами.",
                escalated=True,
                escalation_reason="клиент просит человека / жалоба",
                classification=classification,
            )
        if classification == "spam":
            return TurnResult(text="", classification=classification)

        # kwarg добавляется, только если реально нужен — иначе ломает
        # existing executor_factory-заглушки в тестах, у которых сигнатура
        # (dialog_id, state) без третьего параметра.
        executor = (
            self.executor_factory(dialog_id, state, concessions_blocked=True)
            if concessions_blocked
            else self.executor_factory(dialog_id, state)
        )
        system = build_system_prompt(self.kb)

        # Подсказка о зоне идёт в СОДЕРЖИМОЕ этого хода, а не в системный
        # промт: она меняется от сообщения к сообщению, а кешируемый блок
        # (справочник зон, cache_control) обязан оставаться неизменным байт в
        # байт — иначе кеш промахивается на каждом ходу. history для
        # персистентной истории при этом не трогаем: подсказка только в
        # запросе к модели этим ходом, не в том, что вызывающий код сохранит.
        turn_content = user_text
        if item_id is not None:
            resolution = await resolve_listing(item_id, item_lookup, self.kb)
            hint = build_listing_hint(resolution, self.kb)
            if hint:
                turn_content = f"{user_text}\n\n{hint}"
        elif not history:
            # Чат из профиля продавца (u2u/a2u): объявления нет, зацепки о
            # зоне тоже. Подсказка идёт ТОЛЬКО на первом ходу — дальше
            # клиент уже ответил, чего он хочет, и переспрашивать
            # направление в каждом сообщении незачем.
            turn_content = f"{user_text}\n\n{no_listing_hint()}"

        messages = list(history[-HISTORY_WINDOW:])
        messages.append({"role": "user", "content": turn_content})

        totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
        tool_calls: list[str] = []
        quote_statuses: list[str] = []
        final_text = ""
        hit_limit = False
        tool_call_errors = 0

        for iteration in range(MAX_TOOL_ITERATIONS):
            response = await self.provider.complete(
                model=self.dialog_model,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=TOOLS,
                messages=messages,
            )

            usage = getattr(response, "usage", None)
            if usage is not None:
                totals["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
                totals["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
                totals["cache_read_input_tokens"] += (
                    getattr(usage, "cache_read_input_tokens", 0) or 0
                )

            tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
            if not tool_uses:
                final_text = _text_from_blocks(response.content)
                break

            messages.append({"role": "assistant", "content": response.content})

            results = []
            for block in tool_uses:
                tool_calls.append(block.name)
                payload = await executor.run(block.name, dict(block.input))
                if payload.get("error"):
                    tool_call_errors += 1
                if block.name == "calculate_price" and "status" in payload:
                    quote_statuses.append(payload["status"])
                results.append(tool_result_block(block.id, payload))
            messages.append({"role": "user", "content": results})
        else:
            # Витки кончились, а модель всё ещё зовёт инструменты.
            hit_limit = True
            executor.escalated = True
            executor.escalation_reason = "превышен лимит витков инструментов"
            final_text = "Секунду, уточню детали у менеджера и вернусь с ответом."

        llm_meta = {
            "provider": self.provider.name,
            "model": self.dialog_model,
            **totals,
            "cost_rub": str(
                estimate_cost_rub(
                    self.dialog_model, totals["input_tokens"], totals["output_tokens"]
                )
            ),
            "tool_iterations": len(quote_statuses) or len(tool_calls),
        }

        # Последний рубеж: инструмент ни разу не вызывался за весь ход, а в
        # тексте всё равно всплыла цена. Не имеет значения, какой провайдер
        # это написал и почему — ответ клиенту не уходит.
        if not tool_calls and final_text and PRICE_LIKE_WITHOUT_TOOL_CALL.search(final_text):
            logger.error(
                "guard rail: price-like text without a tool call",
                extra={"dialog_id": dialog_id, "provider": self.provider.name},
            )
            executor.escalated = True
            executor.escalation_reason = GUARD_RAIL_VIOLATION
            return TurnResult(
                text="Секунду, уточню детали у менеджера и вернусь с ответом.",
                escalated=True,
                escalation_reason=GUARD_RAIL_VIOLATION,
                classification=classification,
                tool_calls=tool_calls,
                quote_statuses=quote_statuses,
                granted_offer_templates=list(executor.granted_offer_templates),
                hit_iteration_limit=hit_limit,
                llm_meta=llm_meta,
                tool_call_errors=tool_call_errors,
                concession_state=getattr(executor, "state", None),
                concession_events=getattr(executor, "concession_events", []),
            )

        return TurnResult(
            text=final_text,
            escalated=executor.escalated,
            escalation_reason=executor.escalation_reason,
            classification=classification,
            tool_calls=tool_calls,
            quote_statuses=quote_statuses,
            granted_offer_templates=list(executor.granted_offer_templates),
            hit_iteration_limit=hit_limit,
            llm_meta=llm_meta,
            tool_call_errors=tool_call_errors,
            concession_state=getattr(executor, "state", None),
            concession_events=getattr(executor, "concession_events", []),
        )


def summarize_history(history: list[dict], keep: int = HISTORY_WINDOW) -> list[dict]:
    """Оставляет последние `keep` сообщений, а всё, что старше, сворачивает
    в одну текстовую выжимку — иначе длинный диалог упирается в контекст."""
    if len(history) <= keep:
        return history
    older, recent = history[:-keep], history[-keep:]
    lines = []
    for msg in older:
        content = msg.get("content")
        if isinstance(content, str):
            role = "Клиент" if msg.get("role") == "user" else "Мы"
            lines.append(f"{role}: {content}")
    if not lines:
        return recent
    digest = {
        "role": "user",
        "content": "[Ранее в переписке]\n" + "\n".join(lines[-40:]),
    }
    return [digest, *recent]
