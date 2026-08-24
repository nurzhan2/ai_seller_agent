"""Инструменты агента.

Здесь нет ни одного бизнес-правила. Каждый инструмент — тонкая обёртка над
`app/pricing/` и `app/kb/`: вся арифметика и вся политика уступок живут там,
покрыты 155 тестами, и дублировать их здесь нельзя. Инструмент только
преобразует аргументы модели в вызов и результат — в JSON.

Ключевой инвариант: `calculate_price` возвращает СТАТУС, а не число. Модель
не имеет права превращать `blocked` в сумму — за этим следит и системный
промт, и проверки в тестах прогона (промт №7).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, is_dataclass
from datetime import date as DateType, datetime, time as TimeType
from decimal import Decimal
from typing import Any, Optional

from app.config import get_settings
from app.kb.loader import KnowledgeBase
from app.pricing.concessions import (
    ConcessionRequest,
    DialogConcessionState,
    decide,
)
from app.pricing.engine import PriceQuote, PriceRequest, quote
from app.pricing.quote_gate import apply_dialog_floor

logger = logging.getLogger("parmangal.tools")


# --------------------------------------------------------------------------
# Схемы инструментов для Anthropic API
# --------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_zones",
        "description": (
            "Подобрать подходящие зоны отдыха под количество гостей, формат и дату. "
            "Вызывай, когда клиент описал компанию или спросил, что есть. "
            "Возвращает описание зон — цены здесь НЕТ, для цены нужен calculate_price."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "guests": {"type": "integer", "description": "Количество гостей"},
                "category": {
                    "type": "string",
                    "enum": ["bath", "house", "dome", "grill", "tent", "yurt"],
                    "description": "Тип зоны, если клиент назвал его явно",
                },
                "date": {"type": "string", "description": "Дата в формате YYYY-MM-DD"},
            },
        },
    },
    {
        "name": "calculate_price",
        "description": (
            "Рассчитать стоимость аренды. ЕДИНСТВЕННЫЙ способ узнать цену — "
            "считать в уме или брать цену из истории диалога запрещено. "
            "Возвращает статус: ok (называй сумму), needs_input (спроси клиента о "
            "недостающем), blocked (скажи «уточню у менеджера» и вызови "
            "escalate_to_human, сумму НЕ называй), invalid (объясни причину и "
            "предложи альтернативу)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "zone_id": {"type": "string", "description": "Идентификатор зоны из get_zones"},
                "date": {"type": "string", "description": "Дата в формате YYYY-MM-DD"},
                "start_time": {"type": "string", "description": "Время начала, HH:MM"},
                "hours": {
                    "type": "integer",
                    "description": "Сколько часов. Не указывать, если нужен пакет «весь день»",
                },
                "guests": {"type": "integer"},
                "extras": {
                    "type": "array",
                    "description": "Доп. услуги: [[\"coal\", 1], [\"samovar\", 1]]",
                    "items": {"type": "array"},
                },
                "promo_hint": {
                    "type": "string",
                    "description": "Дословно, если клиент упомянул повод — например «день рождения»",
                },
            },
            "required": ["zone_id", "date"],
        },
    },
    {
        "name": "request_concession",
        "description": (
            "Запросить разрешение на уступку, когда клиент возразил по цене или "
            "замолчал после неё. Ты НЕ решаешь, давать ли скидку — решает система. "
            "Если разрешено, отправляй клиенту offer_template ДОСЛОВНО: менять "
            "условие или цифру нельзя. Если отказано — скидку не предлагай вообще."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "observed_triggers": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["price_objection", "hours_objection", "going_silent", "soft_decline"],
                    },
                    "description": "Что ты наблюдал в словах клиента",
                },
                "client_constraints": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["weekend_only", "date_fixed", "zone_fixed", "hours_fixed"],
                    },
                    "description": "Что клиент зафиксировал и менять не готов",
                },
            },
            "required": ["observed_triggers"],
        },
    },
    {
        "name": "check_availability",
        "description": (
            "Проверить занятость зоны на дату и время. Если вернулся status "
            "\"unknown\" — свободно или нет, мы не знаем: скажи «уточню у менеджера» "
            "и вызови escalate_to_human. Не выдумывай, что свободно."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "zone_id": {"type": "string"},
                "date": {"type": "string"},
                "start_time": {"type": "string"},
                "hours": {"type": "integer"},
            },
            "required": ["zone_id", "date"],
        },
    },
    {
        "name": "get_photos",
        "description": (
            "Получить фотографии зоны и отправить их клиенту. Предлагай фото сам, "
            "не дожидаясь просьбы: в разобранных переписках фото просили четыре "
            "раза и ни разу не прислали, после чего два диалога оборвались."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"zone_id": {"type": "string"}},
            "required": ["zone_id"],
        },
    },
    {
        "name": "save_lead",
        "description": "Сохранить контакт клиента. Вызывай сразу, как получил имя и телефон.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "phone": {"type": "string"},
                "zone_id": {"type": "string"},
                "date": {"type": "string"},
                "guests": {"type": "integer"},
                "notes": {"type": "string"},
            },
            "required": ["phone"],
        },
    },
    {
        "name": "escalate_to_human",
        "description": (
            "Передать диалог живому менеджеру. Вызывай немедленно и без попыток "
            "удержать, если: клиент просит человека, жалуется, груб; котировка "
            "вернула blocked; занятость неизвестна; речь зашла об оплате."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "urgency": {"type": "string", "enum": ["low", "normal", "high"]},
            },
            "required": ["reason"],
        },
    },
    {
        "name": "get_extras",
        "description": "Список дополнительных товаров и услуг с ценами (веники, уголь, шампуры, самовар).",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "answer_from_kb",
        "description": (
            "Ответ на частый вопрос из базы знаний: адрес, река, детская площадка, "
            "что входит бесплатно, можно ли приехать посмотреть, животные. "
            "Если confidence = unknown — ответа у нас нет, эскалируй."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"topic": {"type": "string", "description": "Тема вопроса своими словами"}},
            "required": ["topic"],
        },
    },
]


# --------------------------------------------------------------------------
# Сериализация
# --------------------------------------------------------------------------

def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        # Целые рубли, без экспоненты и хвостов с плавающей точкой.
        return int(value) if value == value.to_integral_value() else str(value)
    if isinstance(value, (DateType, datetime, TimeType)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    return value


def quote_to_dict(q: PriceQuote) -> dict:
    """Урезанное представление котировки для модели.

    `lines` отдаются как готовые строки, а не как числа: так модель не
    соблазняется пересчитать их и вывести производную ставку за час.
    """
    payload: dict[str, Any] = {
        "status": q.status,
        "zone_id": q.zone_id,
        "day_type": q.day_type,
        "warnings": list(q.warnings),
    }
    if q.status == "ok":
        payload["total"] = _jsonable(q.total)
        payload["breakdown"] = q.human_readable
        payload["applied_promo"] = q.applied_promo
        payload["alternative_promos"] = list(q.alternative_promos)
        payload["prepayment"] = _jsonable(q.prepayment)
        payload["requires_concession_tier"] = q.requires_concession_tier
    elif q.status == "blocked":
        payload["blocked_reason"] = q.blocked_reason
        payload["blocking_question_ids"] = list(q.blocking_question_ids)
        payload["instruction"] = (
            "Цену НЕ называй. Скажи, что уточнишь у менеджера, и вызови escalate_to_human."
        )
    elif q.status == "needs_input":
        payload["missing_fields"] = list(q.missing_fields)
        payload["instruction"] = (
            "Задай клиенту вопрос про недостающее. Это НЕ повод эскалировать."
        )
    elif q.status == "invalid":
        payload["reason"] = q.blocked_reason
        payload["suggested_alternatives"] = list(q.suggested_alternatives)
        payload["instruction"] = "Объясни причину и предложи альтернативу."
    return payload


# --------------------------------------------------------------------------
# Исполнение инструментов
# --------------------------------------------------------------------------

def _parse_date(value: Optional[str]) -> Optional[DateType]:
    if not value:
        return None
    try:
        return DateType.fromisoformat(value)
    except ValueError:
        return None


def _parse_time(value: Optional[str]) -> Optional[TimeType]:
    if not value:
        return None
    try:
        hours, _, minutes = value.partition(":")
        return TimeType(int(hours), int(minutes or 0))
    except (ValueError, TypeError):
        return None


_STEM_LENGTH = 4


def _stems(text: str) -> set[str]:
    """Грубые основы слов.

    Полноценная лемматизация здесь избыточна, но точное совпадение слов не
    работает вовсе: клиент пишет «река рыбалка», а в базе «Наличие реки /
    рыбалки / сапов» — ни одно слово не совпадает буквально. Сравнение по
    первым буквам покрывает русские падежи и склонения без словаря.
    """
    cleaned = "".join(ch if ch.isalnum() else " " for ch in text.lower())
    return {word[:_STEM_LENGTH] for word in cleaned.split() if len(word) >= _STEM_LENGTH}


def _topic_score(query: str, topic: str) -> int:
    return len(_stems(query) & _stems(topic))


class ToolExecutor:
    """Исполняет вызовы инструментов в контексте одного диалога.

    Держит `DialogConcessionState`, потому что храповик и лестница уступок —
    свойства диалога, а не отдельного запроса. Состояние читается и пишется в
    БД (`app.db.models.DialogState`), чтобы переживать перезапуск процесса.
    """

    def __init__(
        self,
        kb: KnowledgeBase,
        dialog_id: str,
        state: Optional[DialogConcessionState] = None,
        photo_provider: Any = None,
        lead_sink: Any = None,
        booking_provider: Any = None,
    ):
        self.kb = kb
        self.dialog_id = dialog_id
        self.state = state or DialogConcessionState()
        self.photo_provider = photo_provider
        self.lead_sink = lead_sink
        self.booking_provider = booking_provider
        self.escalated = False
        self.escalation_reason: Optional[str] = None
        self.last_quote: Optional[PriceQuote] = None
        # Дата брони не входит в PriceQuote (котировка описывает стоимость, а
        # не календарь), но нужна движку уступок — он сам проверяет по ней
        # праздник, а не верит булеву флагу от вызывающего кода.
        self.last_booking_date: Optional[DateType] = None
        self.granted_offer_templates: list[str] = []

    async def run(self, name: str, args: dict) -> dict:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return {"error": f"неизвестный инструмент {name!r}"}
        try:
            return await handler(args)
        except Exception as exc:  # noqa: BLE001
            logger.exception("tool failed", extra={"tool": name})
            return {
                "error": f"{type(exc).__name__}: {exc}",
                "instruction": "Не сообщай клиенту об ошибке. Скажи, что уточнишь у менеджера.",
            }

    # -- зоны ---------------------------------------------------------------

    async def _tool_get_zones(self, args: dict) -> dict:
        guests = args.get("guests")
        category = args.get("category")

        result = []
        for zone in self.kb.catalog.zones:
            if category and zone.category.value != category:
                continue
            capacity = zone.capacity.value if zone.capacity.is_resolved() else None
            if guests and capacity is not None and guests > capacity:
                continue
            result.append(
                {
                    "zone_id": zone.id,
                    "name": zone.name,
                    "category": zone.category.value,
                    "capacity": capacity,
                    "capacity_confirmed": zone.capacity.is_resolved(),
                    "description": zone.description.strip(),
                    "includes": zone.includes,
                }
            )
        return {"zones": result, "note": "Цены здесь нет — вызови calculate_price."}

    # -- цена ---------------------------------------------------------------

    async def _tool_calculate_price(self, args: dict) -> dict:
        date_value = _parse_date(args.get("date"))
        if date_value is None:
            return {
                "status": "needs_input",
                "missing_fields": ["date"],
                "instruction": "Спроси у клиента дату.",
            }

        extras = tuple(
            (str(item[0]), int(item[1]) if len(item) > 1 else 1)
            for item in (args.get("extras") or [])
            if isinstance(item, (list, tuple)) and item
        )

        request = PriceRequest(
            zone_id=args.get("zone_id", ""),
            date=date_value,
            start_time=_parse_time(args.get("start_time")),
            hours=args.get("hours"),
            guests=args.get("guests"),
            extras=extras,
            promo_hint=args.get("promo_hint"),
        )

        raw = quote(request, self.kb)
        # ОБЯЗАТЕЛЬНЫЙ шаг: цена не может вырасти выше уже обещанной в этом
        # диалоге. См. README → «Главное правило».
        final = apply_dialog_floor(raw, self.state)
        self.last_quote = final
        self.last_booking_date = date_value

        if final.status == "ok":
            # Регламент Максима: «первое касание — называем цену». Считается
            # только один раз за диалог — пересчёт той же или другой зоны
            # позже НЕ новый первый контакт, поэтому touch_count не сбрасывается
            # обратно, если он уже вырос (например, воркер успел отправить
            # второе касание раньше нового calculate_price).
            new_touch_count = self.state.touch_count if self.state.touch_count > 0 else 1
            self.state = DialogConcessionState(
                base_price_quoted=True,
                used_tiers=self.state.used_tiers,
                floor_reached=self.state.floor_reached,
                touch_count=new_touch_count,
            )
        return quote_to_dict(final)

    # -- уступка ------------------------------------------------------------

    async def _tool_request_concession(self, args: dict) -> dict:
        if self.last_quote is None or self.last_quote.status != "ok":
            return {
                "allowed": False,
                "instruction": "Сначала посчитай цену через calculate_price.",
            }

        observed_triggers = tuple(args.get("observed_triggers") or ())
        touch_max_count = get_settings().touch_max_count
        if "price_objection" in observed_triggers and self.state.touch_count < touch_max_count:
            # Регламент Максима: жалоба на цену эскалирует диалог до предела
            # сама по себе — запланированное «вы где-то затерялись?» позже
            # уже не нужно, разговор о цене идёт живьём прямо сейчас.
            self.state = DialogConcessionState(
                base_price_quoted=self.state.base_price_quoted,
                used_tiers=self.state.used_tiers,
                floor_reached=self.state.floor_reached,
                touch_count=touch_max_count,
            )

        request = ConcessionRequest(
            dialog_id=self.dialog_id,
            quote=self.last_quote,
            observed_triggers=observed_triggers,
            client_constraints=frozenset(args.get("client_constraints") or ()),
            days_until_date=self._days_until(),
            slot_confirmed_free=self._slot_known_free(),
            already_used_tiers=tuple(sorted(self.state.used_tiers)),
            concessions_today=0,
            base_price_quoted=self.state.base_price_quoted,
            floor_reached=self.state.floor_reached,
            touch_count=self.state.touch_count,
            booking_date=self.last_booking_date or DateType.today(),
        )
        decision = decide(request, self.kb)

        if not decision.allowed:
            return {
                "allowed": False,
                "instruction": (
                    "Скидку не предлагай. Продолжай разговор без неё — можешь "
                    "рассказать, что входит в стоимость, или предложить другую дату."
                ),
            }

        used = set(self.state.used_tiers)
        if decision.tier is not None:
            used.add(decision.tier)
        new_floor = self.state.floor_reached
        if decision.new_quote is not None and decision.new_quote.total is not None:
            if new_floor is None or decision.new_quote.total < new_floor:
                new_floor = decision.new_quote.total
        self.state = DialogConcessionState(
            base_price_quoted=self.state.base_price_quoted,
            used_tiers=frozenset(used),
            floor_reached=new_floor,
            touch_count=self.state.touch_count,
        )
        if decision.new_quote is not None:
            self.last_quote = decision.new_quote

        self.granted_offer_templates.append(decision.offer_template)
        return {
            "allowed": True,
            "tier": decision.tier,
            "kind": decision.kind,
            "offer_template": decision.offer_template,
            "instruction": (
                "Отправь offer_template клиенту ДОСЛОВНО. Условие и цифры менять нельзя — "
                "можно только добавить приветствие или закрывающую фразу вокруг."
            ),
            "new_total": _jsonable(decision.new_quote.total) if decision.new_quote else None,
        }

    def _days_until(self) -> int:
        if self.last_booking_date is None:
            # Неизвестная дата не должна открывать уступку — возвращаем
            # заведомо большое число, чтобы условие «близкая дата» не прошло.
            return 999
        return max((self.last_booking_date - DateType.today()).days, 0)

    def _slot_known_free(self) -> bool:
        # Занятость подтверждает провайдер броней, а не модель. Пока провайдера
        # нет (промт №8), считаем неподтверждённым — уступка не выдаётся.
        return False

    # -- занятость ----------------------------------------------------------

    async def _tool_check_availability(self, args: dict) -> dict:
        unknown = {
            "status": "unknown",
            "instruction": (
                "Занятость неизвестна. Скажи, что уточнишь у менеджера, "
                "и вызови escalate_to_human. Не утверждай, что свободно."
            ),
        }
        if self.booking_provider is None:
            return unknown

        booking_date = _parse_date(args.get("date"))
        if booking_date is None:
            return {"status": "needs_input", "missing_fields": ["date"]}

        availability = await self.booking_provider.check_availability(
            zone_id=args.get("zone_id", ""),
            date=booking_date,
            start_time=_parse_time(args.get("start_time")),
            hours=args.get("hours"),
        )
        if not availability.is_known:
            return {**unknown, "reason": availability.reason}
        if availability.status.value == "busy":
            return {
                "status": "busy",
                "free_slots": list(availability.free_slots),
                "instruction": (
                    "Это время занято. Сразу предложи свободное время из free_slots "
                    "или другую зону — не заканчивай разговор отказом."
                ),
            }
        return {"status": "free", "free_slots": list(availability.free_slots)}

    # -- фото ---------------------------------------------------------------

    async def _tool_get_photos(self, args: dict) -> dict:
        zone_id = args.get("zone_id", "")
        if self.photo_provider is None:
            return {
                "photos": [],
                "instruction": (
                    "Фотографий пока нет в системе. Не обещай прислать их — "
                    "предложи приехать посмотреть территорию."
                ),
            }
        photos = await self.photo_provider.get(zone_id)
        return {"photos": photos, "count": len(photos)}

    # -- лид ----------------------------------------------------------------

    async def _tool_save_lead(self, args: dict) -> dict:
        if self.lead_sink is not None:
            await self.lead_sink.save(chat_id=self.dialog_id, **args)
        return {"saved": True}

    # -- эскалация ----------------------------------------------------------

    async def _tool_escalate_to_human(self, args: dict) -> dict:
        self.escalated = True
        self.escalation_reason = args.get("reason")
        return {
            "escalated": True,
            "instruction": (
                "Менеджер уведомлён. Скажи клиенту, что уточнишь и вернёшься с ответом. "
                "Больше ничего не обещай и цену не называй."
            ),
        }

    # -- допы ---------------------------------------------------------------

    async def _tool_get_extras(self, args: dict) -> dict:
        items = []
        for extra in self.kb.catalog.extras:
            entry: dict[str, Any] = {"id": extra.id, "name": extra.name}
            if extra.price is not None and extra.price.is_resolved():
                entry["price"] = extra.price.value
            if extra.weekday_price is not None and extra.weekday_price.is_resolved():
                entry["weekday_price"] = extra.weekday_price.value
            if extra.weekend_price is not None and extra.weekend_price.is_resolved():
                entry["weekend_price"] = extra.weekend_price.value
            items.append(entry)
        return {"extras": items}

    # -- база знаний --------------------------------------------------------

    async def _tool_answer_from_kb(self, args: dict) -> dict:
        best = None
        best_score = 0
        for entry in self.kb.catalog.knowledge:
            if entry.answer is None:
                continue
            score = _topic_score(args.get("topic") or "", entry.question_topic)
            if score > best_score:
                best, best_score = entry, score

        if best is None or best.answer is None:
            return {
                "found": False,
                "confidence": "unknown",
                "instruction": (
                    "Ответа в базе нет. Не придумывай — скажи, что уточнишь у менеджера, "
                    "и вызови escalate_to_human."
                ),
            }
        return {
            "found": True,
            "topic": best.question_topic,
            "answer": best.answer.strip(),
            "confidence": best.confidence,
        }


def tool_result_block(tool_use_id: str, payload: dict) -> dict:
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": json.dumps(_jsonable(payload), ensure_ascii=False),
    }
