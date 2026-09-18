"""Что клиент УЖЕ сказал за весь диалог — разобранное кодом, а не памятью модели.

ЗАЧЕМ. Жалоба заказчика после двух недель в проде, дословно: клиент пишет
«хочу арендовать 20 сентября, есть свободное время и сколько стоит?», а
агент по кругу спрашивает на сколько часов, со скольки и «20 сентября или
октября». Броней через бота — ноль, люди уходят звонить.

История модели передавалась и раньше (app/agent/loop.py:run_turn кладёт
последние HISTORY_WINDOW сообщений в запрос), но полагаться на то, что
модель сама вспомнит число гостей из реплики трёхходовой давности, —
ровно тот класс задач, который в этом проекте уже дважды решался кодом:
даты (app/agent/dates.py) и выбор инструмента (app/agent/tool_forcing.py).
Причина одна и та же: промт модель исполняет через раз, а regex — всегда.

ЧИТАЕМ ТОЛЬКО РЕПЛИКИ КЛИЕНТА. Реплики агента сюда не идут намеренно, по
той же причине, что и в `forced_tool_for`: приветствие агента перечисляет
зоны разом («баня, купол, гриль-домик или шатёр»), и если считать это за
«зона названа», условие выполнялось бы всегда начиная со второго хода, то
есть не значило бы ничего.

ПОСЛЕДНЕЕ УПОМИНАНИЕ ПОБЕЖДАЕТ. «Нас будет 6... хотя нет, 8» — восемь.
Поэтому проход идёт от старых сообщений к новым, а внутри сообщения
берётся последнее совпадение.

ЧЕГО ЭТОТ МОДУЛЬ НАМЕРЕННО НЕ УМЕЕТ — дней недели («в субботу»). Их не
умеет и `app/agent/dates.py`, через который дату разбирает инструмент
`resolve_date`. Научить только эту половину значит получить подсказку
«дата известна: суббота», которую инструмент следом не разберёт, — две
разные правды об одном и том же в одном ходу. Появится разбор дней недели
в dates.py — появится и здесь, одной правкой.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date as DateType
from datetime import time as TimeType
from typing import Optional, Sequence

from app.agent.dates import resolve_relative_date
from app.agent.one_question import is_question, split_sentences
from app.agent.tool_forcing import ZONE_WORDS

# --------------------------------------------------------------------------
# Числительные словами
# --------------------------------------------------------------------------

# «нас четверо», «вшестером» — в переписке это обычнее цифры.
_GUESTS_WORDS: dict[str, int] = {
    "вдвоём": 2, "вдвоем": 2, "двое": 2, "втроём": 3, "втроем": 3, "трое": 3,
    "вчетвером": 4, "четверо": 4, "впятером": 5, "пятеро": 5,
    "вшестером": 6, "шестеро": 6, "всемером": 7, "семеро": 7,
    "ввосьмером": 8, "восьмеро": 8, "вдевятером": 9, "девятеро": 9,
    "вдесятером": 10, "десятеро": 10,
}
_GUESTS_WORDS_RE = re.compile(r"\b(" + "|".join(_GUESTS_WORDS) + r")\b", re.IGNORECASE)

_HOURS_WORDS: dict[str, int] = {
    "пару": 2, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
    "одиннадцать": 11, "двенадцать": 12,
}
_HOURS_WORDS_RE = re.compile(
    r"\bна\s+(" + "|".join(_HOURS_WORDS) + r")\s+час\w*", re.IGNORECASE
)

# --------------------------------------------------------------------------
# Гости
# --------------------------------------------------------------------------

# Единица измерения обязательна везде, кроме связки «нас N»: без неё «20
# сентября» читалось бы как двадцать гостей. Тот же принцип, что и у
# денежного паттерна в app/agent/loop.py — цифра сама по себе не значит
# ничего, значение ей даёт слово рядом.
_GUESTS_RE = re.compile(
    r"\b(\d{1,3})\s*(?:чел\w*|гост\w*|персон\w*|человек\w*)|"
    r"\bнас\s+(?:будет\s+|планируется\s+|где-то\s+|примерно\s+)?(\d{1,3})\b|"
    r"\b(\d{1,2})\s*(?:теро|еро)\b",
    re.IGNORECASE,
)
# Верхняя граница — не «валидация», а защита от чужой цифры в слоте: самая
# вместительная зона каталога — шатёр на 30 человек, всё сильно выше почти
# наверняка не про гостей (год, сумма, номер дома).
_MAX_GUESTS = 200

# --------------------------------------------------------------------------
# Длительность
# --------------------------------------------------------------------------

# «на 4 часа», «часа на 3», «4ч». Предлог «на» — тот самый признак, который
# отличает длительность от времени начала: «на 4 часа» — четыре часа
# аренды, «в 4 часа» — начало в шестнадцать ноль-ноль.
_HOURS_RE = re.compile(
    r"\bна\s+(\d{1,2})\s*(?:час\w*|ч\b)|"
    r"\bчас\w*\s+на\s+(\d{1,2})\b|"
    r"\b(\d{1,2})\s*ч\b",
    re.IGNORECASE,
)
# Голое «4 часа» без предлога — тоже длительность, НО только если перед
# числом не стоит предлог времени. «в 16 часов», «до 22 часов», «с 13
# часов» — это календарь, а не продолжительность.
_BARE_HOURS_RE = re.compile(r"(\S+\s+)?\b(\d{1,2})\s*час\w*", re.IGNORECASE)
_TIME_PREPOSITION = re.compile(r"^(?:в|во|к|ко|до|с|со|около|после|раньше|позже)$", re.IGNORECASE)
_MAX_HOURS = 24

# --------------------------------------------------------------------------
# Время начала
# --------------------------------------------------------------------------

# «с 13 до 17» — сразу и начало, и длительность. Проверяется ПЕРВЫМ:
# по отдельности обе половины выглядят как время начала, и без разбора
# диапазона «до 17» затёрло бы «с 13».
_RANGE_RE = re.compile(
    r"\bс\s*(\d{1,2})(?:[:.](\d{2}))?\s*(?:до|-|–|—)\s*(\d{1,2})(?:[:.](\d{2}))?",
    re.IGNORECASE,
)
# «с 13:00», «в 16.30», «к 18:00»
_TIME_WITH_PREP_RE = re.compile(
    r"\b(?:с|со|в|во|к|ко)\s*(\d{1,2})[:.](\d{2})\b", re.IGNORECASE
)
# «в 16», «с 13 часов», «к 18» — час без минут. Единицы гостей и месяцы
# исключены явно: «в 6 человек» — это не время, «в 20 сентября» — не время.
_HOUR_WITH_PREP_RE = re.compile(
    r"\b(?:с|со|в|во|к|ко)\s*(\d{1,2})\s*(?:час\w*|ч\b)?"
    r"(?!\s*(?:чел|гост|персон|человек|минут|мин\b|янв|фев|мар|апр|ма[йя]|"
    r"июн|июл|авг|сент|окт|ноя|дек|тыс|₽|руб|р\b))"
    r"(?:\s*(утра|вечера|дня|ночи))?",
    re.IGNORECASE,
)
# «16:00» вообще без предлога.
_BARE_COLON_TIME_RE = re.compile(r"\b(\d{1,2}):([0-5]\d)\b")
# «сегодня 16 00» — дословно из инцидента 2026-09-01. Минуты ограничены
# четвертями часа НАМЕРЕННО: без этого «20 09» (двадцатое сентября,
# записанное через пробел) читалось бы как 20:09, то есть дата молча
# превращалась бы во время начала. Реальные брони назначают на круглое
# время, а дата через пробел в переписке встречается постоянно.
_BARE_SPACED_TIME_RE = re.compile(r"\b([01]?\d|2[0-3])\s+(00|15|30|45)\b")


@dataclass(frozen=True)
class Slots:
    """Параметры брони, названные клиентом за весь диалог.

    `date_said` — как их произнёс сам клиент («20 сентября»), `date` — во
    что это разобралось. В подсказке модели нужны оба: первое клиент
    узнаёт, второе не даёт модели пересчитать год самостоятельно (инцидент
    2026-08-29, см. app/agent/dates.py).
    """

    date: Optional[DateType] = None
    date_said: str = ""
    guests: Optional[int] = None
    hours: Optional[int] = None
    start_time: Optional[TimeType] = None
    zone: str = ""
    # Откуда взялась зона: из объявления (клиент пришёл с карточки) или из
    # его собственных слов. В подсказке это разные утверждения, и путать их
    # нельзя: «гриль» — слово клиента, «Гриль-домик («Зелёная зона»)» —
    # название из каталога.
    zone_from_listing: bool = False

    def known(self) -> tuple[str, ...]:
        names = []
        if self.date is not None:
            names.append("date")
        if self.guests is not None:
            names.append("guests")
        if self.hours is not None:
            names.append("hours")
        if self.start_time is not None:
            names.append("start_time")
        if self.zone:
            names.append("zone")
        return tuple(names)


def _guests_in(text: str) -> Optional[int]:
    value: Optional[int] = None
    for match in _GUESTS_RE.finditer(text):
        digits = next((g for g in match.groups() if g), None)
        if digits is None:
            continue
        number = int(digits)
        if 1 <= number <= _MAX_GUESTS:
            value = number
    for match in _GUESTS_WORDS_RE.finditer(text):
        value = _GUESTS_WORDS[match.group(1).lower()]
    return value


def _hours_in(text: str) -> Optional[int]:
    value: Optional[int] = None
    for match in _HOURS_RE.finditer(text):
        digits = next((g for g in match.groups() if g), None)
        if digits is not None and 1 <= int(digits) <= _MAX_HOURS:
            value = int(digits)
    for match in _HOURS_WORDS_RE.finditer(text):
        value = _HOURS_WORDS[match.group(1).lower()]
    for match in _BARE_HOURS_RE.finditer(text):
        previous = (match.group(1) or "").strip()
        if _TIME_PREPOSITION.match(previous):
            continue          # «в 16 часов» — начало, а не длительность
        number = int(match.group(2))
        if 1 <= number <= _MAX_HOURS:
            value = number
    return value


def _time_or_none(hour: int, minute: int = 0) -> Optional[TimeType]:
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return TimeType(hour, minute)
    return None


def _range_in(text: str) -> tuple[Optional[TimeType], Optional[int]]:
    """(начало, длительность) из «с 13 до 17» — или (None, None)."""
    start: Optional[TimeType] = None
    hours: Optional[int] = None
    for match in _RANGE_RE.finditer(text):
        begin = _time_or_none(int(match.group(1)), int(match.group(2) or 0))
        end = _time_or_none(int(match.group(3)), int(match.group(4) or 0))
        if begin is None or end is None:
            continue
        start = begin
        span = (end.hour * 60 + end.minute) - (begin.hour * 60 + begin.minute)
        hours = span // 60 if span > 0 and span % 60 == 0 else None
    return start, hours


def _start_time_in(text: str) -> Optional[TimeType]:
    value: Optional[TimeType] = None
    for match in _BARE_COLON_TIME_RE.finditer(text):
        value = _time_or_none(int(match.group(1)), int(match.group(2))) or value
    for match in _BARE_SPACED_TIME_RE.finditer(text):
        value = _time_or_none(int(match.group(1)), int(match.group(2))) or value
    for match in _TIME_WITH_PREP_RE.finditer(text):
        value = _time_or_none(int(match.group(1)), int(match.group(2))) or value
    for match in _HOUR_WITH_PREP_RE.finditer(text):
        hour = int(match.group(1))
        part = (match.group(2) or "").lower()
        # «в 6 вечера» — восемнадцать ноль-ноль. Без этой поправки слот
        # молча уезжает на двенадцать часов назад.
        if part in ("вечера", "дня") and 1 <= hour <= 11:
            hour += 12
        value = _time_or_none(hour) or value
    return value


def extract_slots(
    history: Optional[Sequence[dict]],
    user_text: str,
    *,
    listing_zone: str = "",
    zone_words: Optional[re.Pattern] = None,
    today: Optional[DateType] = None,
) -> Slots:
    """Слоты из всей переписки: старые сообщения, затем текущее.

    `listing_zone` — зона, однозначно разобранная из объявления
    (app/agent/listing_context.py). Клиент, пришедший с карточки конкретной
    зоны, тем самым уже сказал, о чём речь, — переспрашивать направление
    незачем. Клиентское слово при этом СИЛЬНЕЕ: человек мог прийти с
    объявления бани и спросить про шатёр.
    """
    zone_words = zone_words or ZONE_WORDS

    date: Optional[DateType] = None
    date_said = ""
    guests: Optional[int] = None
    hours: Optional[int] = None
    start_time: Optional[TimeType] = None
    zone = listing_zone
    zone_from_listing = bool(listing_zone)

    messages = [
        (msg.get("content") or "")
        for msg in (history or [])
        if msg.get("role") == "user"
    ]
    messages.append(user_text or "")

    for text in messages:
        if not text.strip():
            continue

        resolution = resolve_relative_date(text, today)
        if resolution is not None:
            date = resolution.date
            date_said = text.strip()

        found_guests = _guests_in(text)
        if found_guests is not None:
            guests = found_guests

        range_start, range_hours = _range_in(text)
        found_hours = _hours_in(text)
        if range_hours is not None:
            hours = range_hours
        elif found_hours is not None:
            hours = found_hours

        found_start = range_start or _start_time_in(text)
        if found_start is not None:
            start_time = found_start

        zone_match = zone_words.search(text)
        if zone_match:
            zone = zone_match.group(0)
            zone_from_listing = False

    return Slots(
        date=date, date_said=date_said, guests=guests,
        hours=hours, start_time=start_time, zone=zone,
        zone_from_listing=zone_from_listing,
    )


# --------------------------------------------------------------------------
# О чём агент уже спрашивал
# --------------------------------------------------------------------------

# Слово в вопросе агента -> слот, о котором этот вопрос. Порядок важен:
# «на какое число и на сколько часов» попадёт в оба списка, и это верно —
# спрошены оба.
_QUESTION_MARKERS: tuple[tuple[str, re.Pattern], ...] = (
    ("date", re.compile(r"како[ем]\s+чис|какую\s+дат|какая\s+дат|на\s+какой\s+день|"
                        r"когда\s+(?:планир|хотит|удобн)", re.IGNORECASE)),
    ("guests", re.compile(r"скольк\w*\s+(?:будет\s+)?(?:гост|человек|чел\b|вас|персон)|"
                          r"на\s+какое\s+количество", re.IGNORECASE)),
    ("hours", re.compile(r"на\s+скольк\w*\s+час|скольк\w*\s+час|как\s+долго|"
                         r"какая\s+длительн", re.IGNORECASE)),
    ("start_time", re.compile(r"со\s+скольк|во\s+скольк|к\s+какому\s+времени|"
                              r"во\s+сколько\s+вас\s+ждать", re.IGNORECASE)),
    ("zone", re.compile(r"какую\s+бан|какая\s+зон|какую\s+зон|что\s+вас\s+интересует|"
                        r"какой\s+формат", re.IGNORECASE)),
)


def asked_slots(history: Optional[Sequence[dict]]) -> tuple[str, ...]:
    """Слоты, о которых агент уже спрашивал в этом чате.

    Смотрим ТОЛЬКО вопросительные предложения реплик агента: «на какое
    число планируете?» — вопрос, «посчитаю на 20 сентября» — нет, хотя
    слова те же.

    Счёт по всей доступной истории, а не по последнему ходу: заказчик
    жаловался именно на круг («спрашивает по кругу»), а круг замыкается
    через два-три хода.
    """
    found: list[str] = []
    for msg in history or []:
        if msg.get("role") != "assistant":
            continue
        for _, sentence in split_sentences(msg.get("content") or ""):
            if not is_question(sentence):
                continue
            for slot, pattern in _QUESTION_MARKERS:
                if slot not in found and pattern.search(sentence):
                    found.append(slot)
    return tuple(found)


# --------------------------------------------------------------------------
# Подсказка модели
# --------------------------------------------------------------------------

_RU_MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)

_SLOT_TITLES = {
    "date": "дата",
    "guests": "гостей",
    "hours": "длительность",
    "start_time": "начало",
    "zone": "зона",
}

# Ровно один вопрос на слот — их и подставляет рубеж занятости, и на них
# же ориентируется модель. Формулировки короткие и без «и»: вопрос с союзом
# «и» — это два вопроса в одном предложении, то есть та же анкета, только
# пролезающая мимо счётчика вопросительных знаков.
SLOT_QUESTIONS = {
    "date": "На какое число планируете?",
    "guests": "Сколько вас будет?",
    "hours": "На сколько часов планируете?",
    "start_time": "Со скольки планируете начать?",
    "zone": "Какая зона вас интересует?",
}

# Порядок, в котором имеет смысл спрашивать недостающее: без даты нельзя
# ничего (ни цена, ни календарь), без зоны не выбрать тариф, длительность
# нужна для суммы, время — для проверки слота, число гостей — последним,
# оно влияет на цену только у шатра.
SLOT_PRIORITY = ("date", "zone", "hours", "start_time", "guests")


def _human_date(value: DateType) -> str:
    return f"{value.day} {_RU_MONTHS_GENITIVE[value.month - 1]}"


def describe_slots(slots: Slots) -> str:
    """«дата — 20 сентября (2026-09-20), гостей — 6» или пустая строка."""
    parts: list[str] = []
    if slots.date is not None:
        parts.append(f"дата — {_human_date(slots.date)} ({slots.date.isoformat()})")
    if slots.zone:
        parts.append(
            f"зона — {slots.zone}" if slots.zone_from_listing
            else f"зона — клиент сказал «{slots.zone}»"
        )
    if slots.hours is not None:
        parts.append(f"длительность — {slots.hours} ч")
    if slots.start_time is not None:
        parts.append(f"начало — {slots.start_time.strftime('%H:%M')}")
    if slots.guests is not None:
        parts.append(f"гостей — {slots.guests}")
    return "; ".join(parts)


def next_missing_slot(slots: Slots, *, needed: Sequence[str] = SLOT_PRIORITY) -> Optional[str]:
    """Первый по важности слот, которого не хватает, — или None.

    Порядок берётся из SLOT_PRIORITY, а не из порядка `needed`: вызывающий
    код перечисляет, ЧТО ему нужно, а не в какой последовательности об этом
    спрашивать — иначе один и тот же диалог спрашивал бы разное в
    зависимости от того, как составлен список у вызывающего.
    """
    known = set(slots.known())
    wanted = set(needed)
    for slot in SLOT_PRIORITY:
        if slot in wanted and slot not in known:
            return slot
    return None


def build_context_hint(slots: Slots, asked: Sequence[str] = ()) -> Optional[str]:
    """Служебная подсказка модели про этот ход — или None, если сказать нечего.

    Идёт в СОДЕРЖИМОЕ хода, а не в системный промт: она меняется от
    сообщения к сообщению, а кешируемый блок промта обязан оставаться
    неизменным байт в байт (см. app/agent/prompts.py).
    """
    lines: list[str] = []

    described = describe_slots(slots)
    if described:
        lines.append(
            f"[служебное] ИЗ ПЕРЕПИСКИ УЖЕ ИЗВЕСТНО: {described}. "
            "Эти параметры НЕ переспрашивай — считай и проверяй по ним."
        )

    repeated = [slot for slot in asked if slot not in slots.known()]
    if repeated:
        titles = ", ".join(_SLOT_TITLES.get(slot, slot) for slot in repeated)
        lines.append(
            f"[служебное] ТЫ УЖЕ СПРАШИВАЛА ЭТО, И ОТВЕТА НЕ БЫЛО: {titles}. "
            "Повторять тот же вопрос нельзя. Дай то, что можешь дать без "
            "него (цену за час, минимум, свободное время), и только потом "
            "задай ОДИН вопрос — другими словами."
        )

    return "\n".join(lines) if lines else None
