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
from datetime import date as DateType, datetime, time as TimeType, timedelta
from decimal import Decimal
from typing import Any, Callable, Optional

from app.agent.dates import resolve_relative_date
from app.config import get_settings
from app.kb.loader import KnowledgeBase
from app.pricing.concessions import (
    ConcessionEvent,
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
        "name": "resolve_date",
        "description": (
            "Превратить дату, названную клиентом словами («29 августа», «завтра», "
            "«15.01»), в формат YYYY-MM-DD. ОБЯЗАТЕЛЬНО вызывай этот инструмент "
            "перед check_availability и calculate_price, если клиент не написал "
            "дату сразу цифрами ISO — год в уме не считай, если он не назван: "
            "код сам возьмёт ближайшую будущую дату. Используй в этих инструментах "
            "ТОЛЬКО дату, которую вернул resolve_date, не переписывай её. Если "
            "получил error — фраза не распознана, переспроси у клиента число."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Фраза клиента с датой, дословно"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "find_next_available",
        "description": (
            "Найти ближайшие свободные даты для зоны — вызывай, когда клиент "
            "просит «ближайшую свободную» и не называет число, или когда "
            "запрошенная дата занята и клиент готов рассмотреть другую. Ищет "
            "вперёд не дальше 14 дней и возвращает первые несколько дат с "
            "хотя бы одним свободным временем. Не заменяет check_availability "
            "на выбранной дате — подтверди ей перед тем, как называть цену."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "zone_id": {"type": "string", "description": "Идентификатор зоны из get_zones"},
                "hours": {"type": "integer", "description": "Сколько часов нужно"},
                "guests": {"type": "integer", "description": "Количество гостей"},
                "from_date": {
                    "type": "string",
                    "description": "С какой даты искать, YYYY-MM-DD. Обычно не указывать — по умолчанию сегодня.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Сколько дат вернуть (по умолчанию 3)",
                },
            },
            "required": ["zone_id"],
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
            "Проверить занятость зоны на дату и время. Дата — строго YYYY-MM-DD; "
            "если клиент назвал её словами, сначала вызови resolve_date и передай "
            "сюда его результат, а не свои вычисления. Если вернулся status "
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
        "name": "create_booking",
        "description": (
            "Поставить бронь в системе бронирования. Вызывай ТОЛЬКО когда "
            "клиент подтвердил всё сразу: зону, дату, время начала и "
            "длительность, и оставил имя с телефоном. До этого — не вызывай. "
            "Перед постановкой инструмент сам перепроверяет занятость, "
            "поэтому отдельный check_availability прямо перед ним не нужен. "
            "Если вернулось booked=false — брони НЕТ: скажи клиенту ровно то, "
            "что написано в instruction, и не выдавай это за подтверждённую бронь."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "zone_id": {"type": "string"},
                "date": {"type": "string", "description": "YYYY-MM-DD, только из resolve_date"},
                "start_time": {"type": "string", "description": "Время начала, HH:MM"},
                "hours": {"type": "integer", "description": "Сколько часов клиент хочет провести"},
                "guests": {"type": "integer"},
                "client_name": {"type": "string", "description": "Имя клиента"},
                "client_phone": {"type": "string", "description": "Телефон клиента"},
                "comment": {"type": "string", "description": "Пожелания клиента, если были"},
            },
            "required": ["zone_id", "date", "start_time", "client_name", "client_phone"],
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

# find_next_available: горизонт поиска и он же потолок числа запросов к
# YCLIENTS за один вызов инструмента (один запрос на день горизонта) — не
# выстрелить сотней обращений за один ход, как просили при разборе живого
# диалога.
FIND_NEXT_AVAILABLE_HORIZON_DAYS = 14
FIND_NEXT_AVAILABLE_DEFAULT_LIMIT = 3


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
        concessions_blocked: bool = False,
        concessions_today_provider: Any = None,
        today_fn: Callable[[], DateType] = DateType.today,
        booking_sink: Any = None,
        booking_notifier: Any = None,
    ):
        self.kb = kb
        self.dialog_id = dialog_id
        self.state = state or DialogConcessionState()
        self.photo_provider = photo_provider
        self.lead_sink = lead_sink
        self.booking_provider = booking_provider
        # Async-колбэк без аргументов -> int, считает уступки ПО ВСЕМ чатам
        # за сегодня (R10, дневной лимит) — тот же приём внедрения, что у
        # booking_provider/photo_provider. Вызывается лениво, внутри
        # _tool_request_concession, а не один раз на весь ход: большинство
        # ходов вообще не доходят до запроса на скидку, и платить лишним
        # походом в БД за каждый из них незачем. None (по умолчанию, и во
        # всех тестах/харнессе, которые его не передают) — 0, то же
        # безопасное вырождение, что и у отсутствующего booking_provider.
        self.concessions_today_provider = concessions_today_provider
        # True для «чистого» повторного хода после таймаута запроса на
        # скидку (app/pipeline.py) — модель ведёт диалог дальше, но
        # request_concession не вызывает decide() вообще, ни при каких
        # условиях не предлагая клиенту скидку в этом ходе.
        self.concessions_blocked = concessions_blocked
        # Только для resolve_date/find_next_available и валидации "дата в
        # прошлом" в check_availability — сделано инъекцией специально ради
        # тестируемости («29 августа» должно резолвиться по фиксированному
        # today, а не по реальным часам машины, где бы тесты ни запускались).
        # _days_until()/_tool_request_concession ниже по файлу по-прежнему
        # берут DateType.today() напрямую — не трогаю их в этой задаче:
        # это отдельная зона риска (движок уступок), не связанная с датами
        # бронирования.
        self._today_fn = today_fn
        # Куда записать поставленную бронь у себя и кого уведомить — тот же
        # приём внедрения, что у lead_sink/photo_provider. None в обоих
        # случаях (тесты, харнесс) не мешает поставить бронь: запись и
        # уведомление важны, но они ПОСЛЕ факта, а не условие для него.
        self.booking_sink = booking_sink
        self.booking_notifier = booking_notifier
        self.escalated = False
        self.escalation_reason: Optional[str] = None
        self.last_quote: Optional[PriceQuote] = None
        # Дата брони не входит в PriceQuote (котировка описывает стоимость, а
        # не календарь), но нужна движку уступок — он сам проверяет по ней
        # праздник, а не верит булеву флагу от вызывающего кода.
        self.last_booking_date: Optional[DateType] = None
        self.last_start_time: Optional[TimeType] = None
        self.last_hours: Optional[int] = None
        # Кеш ответов провайдера за один ход: (zone, date, start, hours) ->
        # Availability. `check_availability` и проверка слота при уступке
        # спрашивают ПРО ОДИН И ТОТ ЖЕ слот в одном ходу — второй сетевой
        # запрос к YCLIENTS не нужен и, что важнее, мог бы вернуть другой
        # ответ, и агент принял бы два решения на разных данных.
        self._availability_cache: dict[tuple, Any] = {}
        self.granted_offer_templates: list[str] = []
        # Каждое решение decide() за ход — не только выданные. Нужно
        # конвейеру, чтобы решить, требует ли ход одобрения оператора
        # (app.pricing.concessions.ConcessionEvent.needs_operator_approval),
        # и чтобы писать ConcessionLog.
        self.concession_events: list[ConcessionEvent] = []

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
        # Время и длительность не входят в PriceQuote (котировка описывает
        # стоимость, а не календарь), но нужны для проверки занятости слота
        # в `_slot_availability` — спрашивать провайдера про зону и дату без
        # времени значит спрашивать не про тот слот, который считали.
        self.last_start_time = request.start_time
        self.last_hours = request.hours

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

        if self.concessions_blocked:
            # Запрос на предыдущую скидку истёк по таймауту — decide() не
            # вызывается вообще, и в concession_events ничего не попадает:
            # это не решение по скидке, а его сознательное отсутствие в
            # этом ходе. См. app/pipeline.py._send_concession_fallback.
            return {
                "allowed": False,
                "instruction": (
                    "Скидку сейчас предложить нельзя. Продолжай разговор без неё — "
                    "можешь рассказать, что входит в стоимость, или предложить другую дату."
                ),
            }

        base_price_before = self.last_quote.total
        concessions_today = 0
        if self.concessions_today_provider is not None:
            concessions_today = await self.concessions_today_provider()
        slot_free, slot_known = await self._slot_availability()
        request = ConcessionRequest(
            dialog_id=self.dialog_id,
            quote=self.last_quote,
            observed_triggers=observed_triggers,
            client_constraints=frozenset(args.get("client_constraints") or ()),
            days_until_date=self._days_until(),
            slot_confirmed_free=slot_free,
            slot_availability_known=slot_known,
            already_used_tiers=tuple(sorted(self.state.used_tiers)),
            concessions_today=concessions_today,
            base_price_quoted=self.state.base_price_quoted,
            floor_reached=self.state.floor_reached,
            touch_count=self.state.touch_count,
            booking_date=self.last_booking_date or DateType.today(),
        )
        decision = decide(request, self.kb)
        self.concession_events.append(
            ConcessionEvent(
                decision=decision,
                base_price=base_price_before,
                zone_id=self.last_quote.zone_id,
                trigger=observed_triggers[0] if observed_triggers else None,
            )
        )

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

    async def _availability_for(
        self, zone_id: str, date_value: DateType,
        start_time: Optional[TimeType], hours: Optional[int],
    ):
        """Ответ провайдера про КОНКРЕТНЫЙ слот, с кешем на один ход.

        Возвращает `Availability` или None, если провайдера нет. Сбой
        провайдера — это UNKNOWN, а не исключение наружу: инструмент,
        падающий из-за недоступности YCLIENTS, останавливает весь ход
        агента, а «не знаю» — законный ответ (см. app/booking/base.py).
        """
        from app.booking.base import Availability, AvailabilityStatus

        if self.booking_provider is None:
            return None

        key = (zone_id, date_value, start_time, hours)
        if key in self._availability_cache:
            return self._availability_cache[key]

        try:
            availability = await self.booking_provider.check_availability(
                zone_id=zone_id, date=date_value, start_time=start_time, hours=hours,
            )
        except Exception:
            logger.exception(
                "booking provider failed, treating the slot as unknown",
                extra={"zone_id": zone_id, "date": str(date_value)},
            )
            availability = Availability(
                status=AvailabilityStatus.UNKNOWN, reason="провайдер недоступен"
            )

        self._availability_cache[key] = availability
        return availability

    async def _slot_availability(self) -> tuple[bool, bool]:
        """(slot_confirmed_free, slot_availability_known) для движка уступок.

        Три состояния провайдера раскладываются в два флага так, как их
        ждёт `ConcessionRequest` (см. его докстринг):
            FREE    -> (True,  True)
            BUSY    -> (False, True)
            UNKNOWN -> (False, False)

        Отсутствие провайдера И отсутствие даты — тоже UNKNOWN, а не
        «занято»: раньше этот метод был захардкожен в False, что означало
        «подтверждённо занято», и R6 отказывал раньше всех остальных
        правил — ценовая уступка не выдавалась НИКОГДА, при том что вся
        механика вокруг выглядела рабочей.
        """
        if self.last_quote is None or self.last_booking_date is None:
            return False, False

        availability = await self._availability_for(
            self.last_quote.zone_id or "",
            self.last_booking_date,
            self.last_start_time,
            self.last_hours,
        )
        if availability is None or not availability.is_known:
            return False, False
        return availability.status.value == "free", True

    # -- даты -----------------------------------------------------------------

    async def _tool_resolve_date(self, args: dict) -> dict:
        resolution = resolve_relative_date(args.get("text") or "", today=self._today_fn())
        if resolution is None:
            return {
                "error": "не удалось распознать дату",
                "instruction": "Переспроси у клиента дату конкретнее — например, «какого числа?».",
            }
        if resolution.date < self._today_fn():
            # Год назван явно и уже прошёл ("29 августа 2025") — это не тот
            # случай, где код домысливает год сам, поэтому ошибка, а не
            # молчаливый перенос на будущее: клиент мог просто опечататься.
            return {
                "error": "указанная дата уже прошла",
                "instruction": "Скажи клиенту, что эта дата уже прошла, и уточни другую.",
            }
        return {"date": resolution.date.isoformat()}

    # -- занятость ----------------------------------------------------------

    async def _tool_check_availability(self, args: dict) -> dict:
        unknown = {
            "status": "unknown",
            "instruction": (
                "Занятость неизвестна. Скажи, что уточнишь у менеджера, "
                "и вызови escalate_to_human. Не утверждай, что свободно."
            ),
        }
        booking_date = _parse_date(args.get("date"))
        if booking_date is None:
            return {"status": "needs_input", "missing_fields": ["date"]}

        # Живой баг: агент сам досчитал «29 августа» до прошлого года и
        # ушёл в YCLIENTS за 2025-08-29 — 422, UNKNOWN, эскалация. Прошлая
        # дата — ошибка инструмента, а не повод спрашивать провайдера то,
        # что и так не имеет смысла спрашивать.
        #
        # ПОРЯДОК: проверка даты идёт ДО проверки провайдера. Дата в прошлом
        # остаётся датой в прошлом независимо от того, подключён ли YCLIENTS,
        # а «unknown + эскалируй» на такой вопрос — это ровно то самое
        # «эскалация при каждой неудачной проверке даты», от которого
        # диалог замолкает вместо того, чтобы переспросить число.
        if booking_date < self._today_fn():
            return {
                "error": "дата уже прошла",
                "instruction": (
                    "Эта дата в прошлом. Не спрашивай занятость и НЕ эскалируй — "
                    "просто переспроси у клиента дату (если он назвал её словами "
                    "без года, например «29 августа», сначала вызови resolve_date)."
                ),
            }

        if self.booking_provider is None:
            return unknown

        # Через тот же кеш, что и проверка слота при уступке: один ход —
        # один ответ провайдера про один слот, иначе агент мог бы сказать
        # клиенту «свободно» и тут же выдать скидку по ответу «занято».
        availability = await self._availability_for(
            args.get("zone_id", ""),
            booking_date,
            _parse_time(args.get("start_time")),
            args.get("hours"),
        )
        if availability is None:
            return unknown
        if not availability.is_known:
            return {**unknown, "reason": availability.reason}
        if availability.status.value == "busy":
            return {
                "status": "busy",
                "free_slots": list(availability.free_slots),
                "instruction": (
                    "Это время занято. Если free_slots не пусто — предложи время "
                    "из него на эту же дату. Если клиент не привязан жёстко к этой "
                    "дате (или free_slots пусто) — вызови find_next_available и "
                    "предложи 2-3 ближайшие свободные даты, или соседнюю подходящую "
                    "зону на то же время — не заканчивай разговор отказом."
                ),
            }
        return {"status": "free", "free_slots": list(availability.free_slots)}

    async def _tool_find_next_available(self, args: dict) -> dict:
        """Идёт по датам вперёд не дальше FIND_NEXT_AVAILABLE_HORIZON_DAYS —
        это же и потолок числа запросов к провайдеру за один вызов
        инструмента, чтобы не выстрелить сотней обращений к YCLIENTS.
        `_availability_for` использует тот же per-ход кеш, что и
        check_availability, поэтому переспрос той же (zone, date) дважды за
        ход не платит вторым сетевым запросом.

        "Свободна" здесь значит "у get_free_slots на эту дату есть хотя бы
        один слот" — НЕ "подтверждено окно нужной длины `hours`": сам
        YClientsProvider.get_free_slots не проверяет непрерывность слотов на
        `hours` часов (см. app/booking/yclients.py), так что и этот
        инструмент не может обещать больше, чем знает провайдер. Финальное
        подтверждение — за check_availability на выбранной дате и времени.
        """
        if self.booking_provider is None:
            return {
                "dates": [],
                "instruction": (
                    "Занятость неизвестна. Скажи, что уточнишь у менеджера, "
                    "и вызови escalate_to_human."
                ),
            }

        zone_id = args.get("zone_id", "")
        guests = args.get("guests")
        zone = next((z for z in self.kb.catalog.zones if z.id == zone_id), None)
        if zone is not None and guests:
            capacity = zone.capacity.value if zone.capacity.is_resolved() else None
            if capacity is not None and guests > capacity:
                return {
                    "dates": [],
                    "instruction": (
                        f"Зона не вмещает {guests} человек (вместимость {capacity}). "
                        "Предложи другую зону через get_zones, а не даты этой."
                    ),
                }

        from_date = _parse_date(args.get("from_date")) or self._today_fn()
        if from_date < self._today_fn():
            from_date = self._today_fn()

        limit = args.get("limit") or FIND_NEXT_AVAILABLE_DEFAULT_LIMIT
        limit = max(1, min(int(limit), FIND_NEXT_AVAILABLE_HORIZON_DAYS))
        hours = args.get("hours")

        found: list[dict] = []
        current = from_date
        for _ in range(FIND_NEXT_AVAILABLE_HORIZON_DAYS):
            if len(found) >= limit:
                break
            availability = await self._availability_for(zone_id, current, None, hours)
            if (
                availability is not None
                and availability.is_known
                and availability.status.value == "free"
                and availability.free_slots
            ):
                found.append({"date": current.isoformat(), "free_slots": list(availability.free_slots)})
            current += timedelta(days=1)

        if not found:
            return {
                "dates": [],
                "instruction": (
                    f"За {FIND_NEXT_AVAILABLE_HORIZON_DAYS} дней вперёд свободных дат "
                    "не нашлось. Скажи об этом клиенту и предложи уточнить у "
                    "менеджера или рассмотреть другую зону."
                ),
            }
        return {
            "dates": found,
            "instruction": (
                "Даты идут по возрастанию. \"Свободна\" здесь значит, что на эту "
                "дату вообще есть открытые слоты — прежде чем называть цену или "
                "фиксировать выбор клиента, подтверди конкретное время через "
                "check_availability."
            ),
        }

    # -- бронирование ---------------------------------------------------------

    async def _tool_create_booking(self, args: dict) -> dict:
        """Ставит бронь — но только после ПОВТОРНОЙ проверки занятости.

        Порядок здесь целиком про то, чтобы не подтвердить клиенту бронь,
        которой нет:

        1. рубильник AUTO_BOOKING_ENABLED — выключен, значит бронь ставит
           человек, и агент не должен обещать её сам;
        2. цена должна быть посчитана в этом же диалоге. Не ради денег: из
           котировки берутся ЧАСЫ ЗАНЯТОСТИ (при акции «6-й час в подарок»
           их 6, а оплаченных 5) — блокировать оплаченные значит отдать
           шестой час другому клиенту;
        3. занятость перепроверяется ЗАНОВО, мимо кеша хода: между «свободно»
           пять реплик назад и этой секундой слот мог уйти;
        4. и только потом — запись в YCLIENTS, в нашу БД и уведомление
           оператору.

        Любой сбой на шагах 1-3 возвращает booked=false с готовой
        формулировкой: инструмент никогда не отдаёт «успех» без реальной
        брони.
        """
        if not get_settings().auto_booking_enabled:
            return {
                "booked": False,
                "instruction": (
                    "Автобронирование выключено. Скажи, что придержишь время и "
                    "менеджер подтвердит, и вызови escalate_to_human."
                ),
            }
        if self.booking_provider is None:
            return {
                "booked": False,
                "instruction": (
                    "Система бронирования недоступна. Скажи, что уточнишь у "
                    "менеджера, и вызови escalate_to_human."
                ),
            }

        booking_date = _parse_date(args.get("date"))
        start_time = _parse_time(args.get("start_time"))
        if booking_date is None or start_time is None:
            return {"booked": False, "status": "needs_input",
                    "missing_fields": [f for f, v in (("date", booking_date), ("start_time", start_time)) if v is None],
                    "instruction": "Уточни у клиента недостающее и повтори."}
        if booking_date < self._today_fn():
            return {
                "booked": False,
                "error": "дата уже прошла",
                "instruction": "Эта дата в прошлом. Не эскалируй — переспроси дату у клиента.",
            }

        quote_for_hours = self.last_quote
        if quote_for_hours is None or quote_for_hours.status != "ok":
            return {
                "booked": False,
                "instruction": (
                    "Сначала посчитай цену через calculate_price — без неё "
                    "неизвестно, сколько часов занимать."
                ),
            }
        occupied_hours = quote_for_hours.occupied_hours or args.get("hours")
        if not occupied_hours:
            return {
                "booked": False,
                "status": "needs_input",
                "missing_fields": ["hours"],
                "instruction": "Уточни у клиента, на сколько часов он планирует.",
            }

        zone_id = args.get("zone_id", "")
        # Мимо кеша хода — см. докстринг, п.3. `_availability_cache` живёт
        # ровно один ход и существует ради согласованности внутри него; для
        # брони важнее свежесть, поэтому запись о слоте выбрасывается и
        # спрашивается заново. Обновлённый ответ снова попадает в кеш, так
        # что остальные проверки в этом же ходу увидят ту же картину.
        self._availability_cache.pop((zone_id, booking_date, start_time, occupied_hours), None)
        availability = await self._availability_for(
            zone_id, booking_date, start_time, occupied_hours
        )
        if availability is None or not availability.is_known:
            return {
                "booked": False,
                "status": "unknown",
                "instruction": (
                    "Занятость сейчас не подтверждается — бронь НЕ поставлена. "
                    "Скажи, что уточнишь у менеджера, и вызови escalate_to_human."
                ),
            }
        if availability.status.value == "busy":
            return {
                "booked": False,
                "status": "busy",
                "free_slots": list(availability.free_slots),
                "instruction": (
                    "Пока договаривались, время заняли — брони НЕТ. Извинись, "
                    "предложи время из free_slots или вызови find_next_available "
                    "и предложи ближайшие свободные даты. Не эскалируй."
                ),
            }

        from app.booking.base import BookingRequest

        result = await self.booking_provider.create_booking(
            BookingRequest(
                zone_id=zone_id,
                date=booking_date,
                start_time=start_time,
                occupied_hours=int(occupied_hours),
                guests=args.get("guests") or 0,
                client_name=args.get("client_name"),
                client_phone=args.get("client_phone"),
                comment=args.get("comment"),
            )
        )
        if not result.success:
            logger.warning(
                "booking failed", extra={"chat_id": self.dialog_id, "error": result.error},
            )
            return {
                "booked": False,
                "error": result.error,
                "instruction": (
                    "Бронь не поставилась. Скажи, что уточнишь у менеджера и "
                    "вернёшься, и вызови escalate_to_human."
                ),
            }

        record = {
            "chat_id": self.dialog_id,
            "record_id": result.booking_id,
            "zone_id": zone_id,
            "booking_date": booking_date,
            "start_time": start_time.strftime("%H:%M"),
            "occupied_hours": int(occupied_hours),
            "billable_hours": quote_for_hours.billable_hours,
            "guests": args.get("guests"),
            "total": quote_for_hours.total,
            "client_name": args.get("client_name"),
            "client_phone": args.get("client_phone"),
            "applied_promo": quote_for_hours.applied_promo,
        }
        # Запись у себя и уведомление оператору — ПОСЛЕ успеха в YCLIENTS и
        # каждое в своём try: бронь уже стоит, и уронить ход из-за того, что
        # не записалось в нашу таблицу или не ушло в Telegram, значит
        # оставить клиента без подтверждения при существующей брони.
        if self.booking_sink is not None:
            try:
                await self.booking_sink.save(**record)
            except Exception:
                logger.exception("booking saved in YCLIENTS but not in our DB",
                                 extra={"chat_id": self.dialog_id, "record_id": result.booking_id})
        if self.booking_notifier is not None:
            try:
                await self.booking_notifier(record)
            except Exception:
                logger.exception("booking notification failed",
                                 extra={"chat_id": self.dialog_id})

        logger.info("booking created",
                    extra={"chat_id": self.dialog_id, "record_id": result.booking_id,
                           "zone_id": zone_id, "occupied_hours": int(occupied_hours)})
        return {
            "booked": True,
            "record_id": result.booking_id,
            "occupied_hours": int(occupied_hours),
            "instruction": (
                "Время придержано. Скажи клиенту, что придержала время и менеджер "
                "свяжется для подтверждения. Слова «забронировал», «бронь "
                "подтверждена», «место за вами» по-прежнему запрещены."
            ),
        }

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
