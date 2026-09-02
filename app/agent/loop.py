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

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
from app.agent.tool_forcing import forced_tool_for
from app.agent.tools import TOOLS, ToolExecutor, tool_result_block
from app.clock import moscow_now
from app.kb.loader import KnowledgeBase

logger = logging.getLogger("parmangal.loop")

MAIN_MODEL = "claude-sonnet-5"
CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"

# Сколько символов вызова инструмента писать в лог и хранить в llm_meta.
# Ответ get_zones или каталог цен занимают килобайты — целиком они делают
# лог нечитаемым, а строку `messages` тяжёлой; обрезанного хвоста хватает,
# чтобы понять, что именно вернул инструмент.
TOOL_TRACE_LIMIT = 600

# Имена, которые мы объявили модели. Всё, что приходит помимо, —
# мусор слоя совместимости, а не наш инструмент: DeepSeek 2026-09-02
# вернул блок tool_use с именем "tool_calls", которого у нас нет.
KNOWN_TOOL_NAMES = frozenset(t["name"] for t in TOOLS)


def normalize_tool_use(block: Any) -> tuple[str, dict, str]:
    """Имя, аргументы и id из блока tool_use — каким бы он ни пришёл.

    ЗАЧЕМ. Блок приходит от ЧУЖОГО слоя совместимости, и его форма не наша
    гарантия. DeepSeek 2026-09-02 прислал tool_use с именем "tool_calls" —
    это имя обёртки из OpenAI-формата, а не инструмента. Раз протёк ключ
    обёртки, протечь может и её содержимое: в OpenAI-форме аргументы лежат
    СТРОКОЙ JSON, а не объектом. Прямой `dict(block.input)` на такой строке
    поднимает ValueError, и ход клиента умирает целиком — из-за чужой
    оплошности в поле, которое мы даже не собирались исполнять.

    Правило простое: разобрать что можно, остальное превратить в пустые
    аргументы и отдать `ToolExecutor.run`. Он на незнакомое имя вернёт
    {"error": ...}, модель получит ошибку и на следующем витке позовёт
    заново — это штатная петля, а не сбой.

    Возвращает (имя, аргументы, id). Пустое имя — тоже допустимый результат:
    исполнитель на него честно ответит «неизвестный инструмент».
    """
    name = getattr(block, "name", None)
    name = name if isinstance(name, str) else ""

    raw = getattr(block, "input", None)
    if isinstance(raw, dict):
        arguments = dict(raw)
    elif isinstance(raw, str):
        # OpenAI-форма: arguments — строка JSON. Если она разбирается в
        # объект, аргументы настоящие, и терять их незачем.
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
        arguments = dict(parsed) if isinstance(parsed, dict) else {}
    else:
        arguments = {}

    block_id = getattr(block, "id", None)
    if not isinstance(block_id, str) or not block_id:
        # id нужен только для того, чтобы связать результат с вызовом. Своя
        # заглушка честнее падения: ответ модели всё равно будет про ошибку.
        block_id = "unknown_tool_use"

    return name, arguments, block_id

MAX_TOOL_ITERATIONS = 5
MAX_TOKENS = 1024
HISTORY_WINDOW = 30
# MAX_AGENT_REPLIES_PER_CHAT здесь НЕТ намеренно. Константа с таким именем
# лежала в этом файле и не использовалась ни разу — из-за неё лимит
# выглядел захардкоженным, хотя решение принимает
# OpsService.should_agent_reply по настройке
# Settings.max_agent_replies_per_chat (AGENT_MAX_REPLIES_PER_CHAT).

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

# ТРЕТИЙ РУБЕЖ: СУММА ДОЛЖНА БЫТЬ ИЗ ОТВЕТА ИНСТРУМЕНТА, А НЕ «ПОХОЖЕЙ НА
# ПРАВДУ». Перенесено из харнесса (app/quality/asserts.py, правило
# price_mismatch), где эта проверка жила с самого начала и ловила то, чего
# рантайм не ловил: первый рубеж спрашивает «был ли вызов вообще», и его
# устраивает ЛЮБОЙ вызов. Полный прогон 2026-09-02 показал, зачем нужен
# второй вопрос: агент вызвал calculate_price с выдуманными zone_id и
# сегодняшней датой, а клиент спрашивал про другое.
#
# СВЕРЯЕМСЯ СО ВСЕМИ ЧИСЛАМИ ИЗ ОТВЕТОВ ИНСТРУМЕНТОВ, А НЕ ТОЛЬКО С `total`.
# Дословный перенос правила харнесса («любая сумма обязана равняться total»)
# зарубил бы два ПРАВИЛЬНЫХ ответа из того же прогона:
#
#   «Юрта на сутки — 4000 ₽, предоплата 3000 ₽»  — 3000 это `prepayment`;
#   «набор из 6 штук за 500 ₽»                   — цена из get_extras.
#
# Обе цифры пришли от инструментов, обе верные, и глушить их незачем.
# Инвариант, который мы на самом деле защищаем, — «агент не выдумывает
# цифры», а не «в ответе ровно один total».
_NUMBER_IN_TEXT = re.compile(r"\d[\d\s\u00a0]*(?:[.,]\d+)?")


def _canonical_amount(raw: str) -> str:
    """«10 500», «10500.00», «10500,0» -> «10500».

    Одна форма для чисел из текста и из ответа инструмента: без неё сверка
    разошлась бы на форматировании, а не на смысле.
    """
    cleaned = raw.replace("\u00a0", "").replace(" ", "").replace(",", ".")
    if "." in cleaned:
        whole, _, fraction = cleaned.partition(".")
        fraction = fraction.rstrip("0")
        cleaned = f"{whole}.{fraction}" if fraction else whole
    return cleaned.lstrip("0") or "0"


def amounts_in_payload(value: Any) -> set[str]:
    """Все числа, которые инструмент отдал агенту, — на любой глубине.

    Ходим по структуре целиком: суммы лежат и полями (`total`), и внутри
    строк («Аренда шампуров — 500 ₽ за набор»), и в списках позиций.
    """
    found: set[str] = set()
    if isinstance(value, dict):
        for item in value.values():
            found |= amounts_in_payload(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            found |= amounts_in_payload(item)
    elif isinstance(value, bool):
        pass                      # bool — подкласс int, числом здесь не считается
    elif isinstance(value, (int, float, Decimal)):
        found.add(_canonical_amount(str(value)))
    elif isinstance(value, str):
        for match in _NUMBER_IN_TEXT.finditer(value):
            found.add(_canonical_amount(match.group()))
    return found


def invented_amounts(text: str, allowed: set[str]) -> list[str]:
    """Суммы из текста, которых нет ни в одном ответе инструмента.

    Смотрим только на то, что выглядит как ДЕНЬГИ (цифры рядом с «₽», «руб»,
    «р.»), — тем же паттерном, что и первый рубеж. Число часов, число гостей
    и время сюда не попадают: они не про деньги, и сверять их не с чем.
    """
    bad: list[str] = []
    for match in PRICE_LIKE_WITHOUT_TOOL_CALL.finditer(text or ""):
        digits = _NUMBER_IN_TEXT.search(match.group())
        if digits is None:
            continue
        if _canonical_amount(digits.group()) not in allowed:
            bad.append(match.group().strip())
    return bad


AMOUNT_MISMATCH_VIOLATION = (
    "сумма в тексте не из ответа инструмента (последний рубеж)"
)

# ВТОРОЙ ПОСЛЕДНИЙ РУБЕЖ: занятость и даты. Заведён по инциденту запуска
# 2026-09-01.
#
# Клиент: «Есть окошко сегодня?» (22:52). Ответ (22:53, tool_trace ПУСТОЙ):
# «...сейчас 22:53, и окошко на 14:00 давно закрыто. Ближайшие свободные —
# завтра, 1 сентября, весь день». Сегодня БЫЛО 1 сентября; никакого «14:00»
# клиент не называл; свободных слотов никто не проверял. Ценовой рубеж это
# пропустил — и правильно, цены в тексте нет.
#
# ДВА РАЗНЫХ ПРАВИЛА, А НЕ ОДНО. Первая редакция блокировала любой текст с
# датой без вызова инструмента — и на прогоне сразу срезала правильный ответ
# «завтра будет 2 сентября», для которого никакой инструмент не нужен: дата
# лежит в промте. Утверждение о ЗАНЯТОСТИ и называние ДАТЫ — разные вещи, и
# проверяются по-разному:
#
#   занятость  — нужен инструмент, потому что этих данных в промте нет;
#   дата       — инструмент не нужен, она в промте есть; проверяем не факт
#                вызова, а СОВПАДЕНИЕ с тем, что модели показали.
AVAILABILITY_TOOLS = frozenset({"check_availability", "find_next_available"})

# Словарь занятости. Намеренно без «дата», «число», «время» самих по себе:
# «на какую дату планируете?» — вопрос, а не утверждение о календаре.
AVAILABILITY_WORDS = re.compile(
    r"\b(?:свободн\w*|занят\w*|окошк\w*|окно\b|есть\s+врем\w*|ближайш\w*)",
    re.IGNORECASE,
)

# ОБЕЩАНИЕ ПРОВЕРИТЬ — НЕ УТВЕРЖДЕНИЕ О ЗАНЯТОСТИ.
#
# Прод 2026-09-02: рубеж зарубил честный ответ модели «уточню свободное время
# на сегодня вечером — и вернусь с вариантами». Слово «свободное» в нём есть,
# но никакого факта о календаре он не сообщает: это намерение посмотреть.
# Клиент из-за этого получил третью подряд одинаковую отписку.
#
# Ловим глагол намерения ПЕРЕД словом занятости, в пределах короткого окна:
# «уточню свободное» — обещание, «свободное время есть» — утверждение.
#
# ФОРМА ГЛАГОЛА — ЧАСТЬ ПРАВИЛА, НО ОДНОЙ ФОРМЫ МАЛО.
#
# Первая редакция ловила основу («посмотр\\w*») и считала обещанием и
# «посмотрю», и «посмотрела — завтра всё занято». Вторая перечислила
# окончания намерения и стала считать УТВЕРЖДЕНИЕМ всё прошедшее — и на
# полном прогоне 2026-09-02 срезала четыре честных ответа из девяти
# сработавших, все одного вида:
#
#   «...чтобы я ПРОВЕРИЛА свободное время и посчитала стоимость»
#   «...чтобы я сразу ПРОВЕРИЛА свободные даты»
#
# По форме это прошедшее время, по смыслу — намерение: русская придаточная
# цели «чтобы + глагол на -ла» описывает то, что ещё не сделано. Отличить их
# по одному слову нельзя, и притворяться, что можно, — значит выбирать, какую
# из двух ошибок совершать.
#
# Правило поэтому в два шага:
#   1. основа глагола намерения слева — как в первой редакции;
#   2. если форма ПРОШЕДШАЯ, нужен ещё маркер цели («чтобы», «смогу»,
#      «могла», «давайте»). Есть маркер — обещание; нет — доклад о
#      календаре, и ему без вызова инструмента хода нет.
#
# Инфинитив («смогу ПРОВЕРИТЬ занятость») прошедшим не является и проходит
# первым же шагом.
PROMISE_VERBS = re.compile(
    r"\b(?:уточн|посмотр|смотр|провер|узна|глян|подбер|подскаж)\w*",
    re.IGNORECASE,
)
_PAST_FORM = re.compile(r"(?:л|ла|ло|ли)$", re.IGNORECASE)
_PURPOSE = re.compile(
    r"\b(?:чтобы|чтоб|смог\w*|мог\w*|давайте|давай|хочу|надо|нужно|сейчас)\b",
    re.IGNORECASE,
)

# УСЛОВИЕ И ВОПРОС — ТОЖЕ НЕ УТВЕРЖДЕНИЕ.
#
# Прогон 2026-09-02, дословный ответ модели: «Баню можно взять отдельной
# зоной, ЕСЛИ СВОБОДНА. Давайте я проверю занятость шатра». Рубеж срезал его
# целиком — а он ничего о календаре не сообщает: «если свободна» это
# оговорка, ровно противоположная утверждению «свободна».
#
# Сюда же «свободна ЛИ», «занято ЛИ» — вопрос, а не ответ.
_CONDITIONAL_BEFORE = re.compile(r"\b(?:если|когда|вдруг|при\s+условии)\b\s*$",
                                 re.IGNORECASE)
_QUESTION_AFTER = re.compile(r"^\w*\s*(?:ли)\b", re.IGNORECASE)

# «БЛИЖАЙШИЙ» НЕ ВСЕГДА ПРО ЗАНЯТОСТЬ. Прогон 2026-09-02 дал четыре таких
# ответа, все срезанные рубежом, и все — про календарь как таковой:
#
#   «Ближайшее воскресенье — 6 сентября»          (какой это день)
#   «ближайший август уже прошёл»                 (какой это месяц)
#   «...вы имеете в виду ближайшее число?»        (ВОПРОС клиенту)
#   «...ближайшие выходные (5-6 сентября)?»       (ВОПРОС клиенту)
#
# Первые два разбираются по слову следом, вторые два — по вопросительному
# знаку: спросить, какое число клиент имеет в виду, — это не утверждение о
# занятости ни в каком виде.
#
# Слово остаётся в словаре ради «ближайшие даты — 5 и 6 сентября», где факт о
# занятости есть, а слова «свободно» нет. Исключение по вопросу нарочно
# СУЖЕНО до «ближайшего» и не распространено на остальной словарь: «Завтра
# всё занято, перенесём?» — тоже вопрос, но утверждение в нём есть.
_NEAREST_ABOUT_A_DAY = re.compile(
    r"^\w*\s+(?:понедельник|вторник|сред|четверг|пятниц|суббот|воскресень|"
    r"январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"[.!?]")


def _nearest_is_not_about_our_calendar(right: str) -> bool:
    """«ближайшее воскресенье», «ближайшее число?» — не про наши слоты."""
    if _NEAREST_ABOUT_A_DAY.match(right):
        return True
    end = _SENTENCE_END.search(right)
    return end is not None and end.group() == "?"

# Сколько символов между глаголом и словом занятости считаем одной фразой.
# «уточню свободное время» — 7; «посмотрю по календарю свободное» — 22.
#
# Для условия отдельного окна НЕТ намеренно: `_CONDITIONAL_BEFORE` привязан к
# концу строки (`\s*$`), то есть требует «если» вплотную к слову занятости, и
# ширина окна на результат не влияет. Такая константа была — мутационный
# разбор показал, что её можно менять с 16 на 200 и ни один тест не заметит.
# Настройка, которая ничего не настраивает, хуже отсутствующей: следующий
# читатель будет её крутить.
_PROMISE_WINDOW = 30


def _promises_to_check(left: str) -> bool:
    """Обещает ли левая часть фразы ПОСМОТРЕТЬ (а не докладывает результат).

    Разбор форм — в комментарии к PROMISE_VERBS выше.
    """
    matches = list(PROMISE_VERBS.finditer(left))
    if not matches:
        return False
    verb = matches[-1].group()
    if not _PAST_FORM.search(verb):
        return True                      # «уточню», «проверить», «уточните»
    return bool(_PURPOSE.search(left))    # «чтобы я проверила» — ещё не сделано


def availability_claim(text: str) -> bool:
    """Утверждает ли текст что-то о занятости (а не обещает или спрашивает).

    True — есть слово занятости, и хотя бы одно из них НЕ прикрыто ни
    обещанием посмотреть, ни условием, ни вопросительной частицей, ни
    разговором про сам календарь («ближайшее воскресенье»).
    Достаточно одного неприкрытого: «уточню свободное время, но завтра всё
    занято» — второе слово и есть нарушение.
    """
    text = text or ""
    for m in AVAILABILITY_WORDS.finditer(text):
        left = text[max(0, m.start() - _PROMISE_WINDOW):m.start()]
        # Справа смотрим до конца предложения: вопросительный знак у
        # «ближайшего» стоит в его конце, а не через пару слов.
        right = text[m.end():m.end() + 120]
        if _promises_to_check(left):
            continue
        if _CONDITIONAL_BEFORE.search(left):
            continue
        if _QUESTION_AFTER.match(right):
            continue
        if m.group().lower().startswith("ближайш") and _nearest_is_not_about_our_calendar(right):
            continue
        return True
    return False

_MONTHS_RU = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
# «сегодня»/«завтра»/«послезавтра» -> сдвиг в днях от сегодняшнего.
_RELATIVE_DAYS = {"сегодня": 0, "завтра": 1, "послезавтра": 2}
_RELATIVE_RE = re.compile(r"\b(сегодня|завтра|послезавтра)\b", re.IGNORECASE)
_DATE_RE = re.compile(
    r"\b(\d{1,2})\s*[.\-/]\s*(\d{1,2})(?:\s*[.\-/]\s*\d{2,4})?\b"
    r"|\b(\d{1,2})\s+(январ|феврал|март|апрел|ма|июн|июл|август|сентябр|"
    r"октябр|ноябр|декабр)\w*",
    re.IGNORECASE,
)
# Насколько близко к слову «завтра» должно стоять число, чтобы считать их
# одной фразой. «Ближайшие свободные — завтра, 1 сентября» укладывается с
# запасом; отдельные упоминания в разных абзацах не слипаются.
_PAIRING_WINDOW = 40


def _dates_in(text: str) -> list[tuple[int, int, int]]:
    """(позиция, день, месяц) для каждой найденной календарной даты."""
    found = []
    for m in _DATE_RE.finditer(text):
        if m.group(1):
            day, month = int(m.group(1)), int(m.group(2))
        else:
            day = int(m.group(3))
            key = m.group(4).lower()
            month = next((v for k, v in _MONTHS_RU.items() if key.startswith(k)), 0)
        if 1 <= day <= 31 and 1 <= month <= 12:
            found.append((m.start(), day, month))
    return found


def date_contradicts_now(text: str, now: datetime) -> Optional[str]:
    """«завтра, 1 сентября» в день, когда 1 сентября — сегодня.

    Сверяем не факт вызова инструмента, а само число: обе даты модели уже
    показаны в блоке «Сейчас:» (app/agent/prompts.py:build_now_block), и
    расхождение означает, что она их не прочитала. Возвращает описание для
    лога либо None.

    Осознанные ограничения. Год не проверяем: в переписке его почти не пишут,
    а «1 сентября» без года — это ближайшее 1 сентября, и разбирать это здесь
    значило бы повторять resolve_date. Пары ищем в окне символов, а не по
    синтаксису: разбирать русский текст правилами дороже, чем ошибиться в
    сторону пропуска — рубеж не единственная защита, а последняя.
    """
    dates = _dates_in(text)
    if not dates:
        return None
    for m in _RELATIVE_RE.finditer(text):
        word = m.group(1).lower()
        expected = (now + timedelta(days=_RELATIVE_DAYS[word])).date()

        # ТОЛЬКО БЛИЖАЙШАЯ ДАТА, А НЕ ВСЕ В ОКНЕ. Прогон 2026-09-02, дословный
        # ответ модели: «сегодня 2 сентября, и 20 июня действительно уже
        # позади» — САМОКОРРЕКЦИЯ, и она верна. Прежняя редакция сверяла
        # «сегодня» с каждой датой окна, натыкалась на 20 июня и рубила
        # правильный текст. Расшифровывает относительное слово всегда
        # соседнее число; всё, что дальше, — уже про другое.
        near = [(abs(pos - m.start()), pos, day, month) for pos, day, month in dates
                if abs(pos - m.start()) <= _PAIRING_WINDOW]
        if not near:
            continue
        _, _, day, month = min(near)
        if (day, month) != (expected.day, expected.month):
            return (
                f"«{word}» рядом с {day:02d}.{month:02d}, "
                f"а {word} — {expected.strftime('%d.%m')}"
            )
    return None


AVAILABILITY_GUARD_VIOLATION = (
    "занятость без вызова check_availability/find_next_available (последний рубеж)"
)
DATE_GUARD_VIOLATION = "дата в тексте противоречит блоку «Сейчас:» (последний рубеж)"
#
# ОТВЕТ НАРАСТАЕТ, А НЕ ПОВТОРЯЕТСЯ. Прод 2026-09-02: клиент получил ОДНО И
# ТО ЖЕ сообщение три раза подряд за четыре минуты, отвечая на каждое. Он
# успел назвать и дату, и время, и число гостей — а в ответ каждый раз тот же
# вопрос. Три одинаковых сообщения хуже, чем позвать человека.
#
# Первый раз — вопрос. Второй — он же другими словами. Третий — оператор.
#
# НИ ОДИН ИЗ ТЕКСТОВ НЕ ОБЕЩАЕТ ВЕРНУТЬСЯ. Агент первым не пишет
# (TOUCH_ENABLED=false, см. app/config.py), поэтому «вернусь с вариантами» —
# обещание, которое он физически не может исполнить: следующий ход обязан
# быть за клиентом.
#
# И ни один не попадает под собственные правила рубежа — иначе подстановка
# сама выглядела бы как утверждение о календаре. Держится тестом
# test_the_guard_replies_do_not_trip_the_guard_itself.
AVAILABILITY_GUARD_REPLIES = (
    "Подскажите, пожалуйста, на какое число и на сколько гостей вы "
    "рассчитываете?",
    "Чтобы посмотреть по календарю, мне нужны точная дата и количество "
    "гостей — напишите их, пожалуйста.",
)
# Третье срабатывание подряд: разговор не двигается, зовём человека.
AVAILABILITY_GUARD_HANDOFF = (
    "Секунду, подключаю менеджера — он посмотрит по календарю и ответит вам."
)
AVAILABILITY_GUARD_HANDOFF_REASON = (
    "последний рубеж сработал третий раз подряд — модель не может ответить "
    "по датам и занятости"
)
_GUARD_TEXTS = frozenset(AVAILABILITY_GUARD_REPLIES) | {AVAILABILITY_GUARD_HANDOFF}


def guard_repeats(history: Optional[list[dict]]) -> int:
    """Сколько раз подряд рубеж уже подставлял ответ в конце этого диалога.

    Считаем по истории, а не по отдельному счётчику в БД: подстановки и так
    лежат в переписке как сообщения агента, а второй источник того же числа
    рано или поздно разойдётся с первым.

    Реплики клиента между подстановками счёт НЕ сбрасывают — он как раз и
    отвечает на каждую, ровно это и было в проде.
    """
    n = 0
    for msg in reversed(history or []):
        if msg.get("role") != "assistant":
            continue
        if (msg.get("content") or "").strip() in _GUARD_TEXTS:
            n += 1
        else:
            break
    return n


@dataclass
class TurnResult:
    text: str
    escalated: bool = False
    escalation_reason: Optional[str] = None
    classification: Optional[str] = None
    tool_calls: list[str] = field(default_factory=list)
    quote_statuses: list[str] = field(default_factory=list)
    # Числа из ответов инструментов за этот ход. Наружу они нужны харнессу
    # качества: он проверяет тот же инвариант («агент не выдумывает цифры»)
    # тем же кодом, а не собственной копией правила — две копии одного
    # правила расходятся молча.
    tool_amounts: set[str] = field(default_factory=set)
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


def _loggable(value: Any) -> Any:
    """Значение, пригодное и для лога, и для JSONB: сериализуемое и короткое.

    Аргументы инструментов приходят от модели и содержат только скаляры, но
    результаты — наши, и в них попадают Decimal, date и вложенные структуры
    на килобайты. `default=str` вместо падения на несериализуемом: журнал
    вызовов не должен ронять ход клиента ради собственной аккуратности.
    """
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return {"_нечитаемо": type(value).__name__}
    if len(text) <= TOOL_TRACE_LIMIT:
        return json.loads(text)
    return {"_обрезано": text[:TOOL_TRACE_LIMIT]}


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
        photo_provider: Any = None,         # .get(zone_id) -> list[image_id], см. app/media/photos.py
        concessions_today_provider: Any = None,   # async () -> int, R10 дневной лимит
        booking_sink: Any = None,           # .save(**record) — запись брони в нашу БД
        booking_notifier: Any = None,       # async (record) -> None — уведомление оператору
        # async (card) -> None — карточка «поставьте бронь руками» на этапе
        # оплаты (payment.handoff_on_payment_step). Не то же самое, что
        # booking_notifier: там бронь уже стоит, здесь её ещё нет.
        booking_handoff_notifier: Any = None,
        # DailyCostGuard (app/metrics.py) — предохранитель по расходу на
        # модели. Живёт ЗДЕСЬ, а не в конвейере, потому что это единственная
        # точка, через которую проходит каждый платный вызов модели: и
        # обычный ход, и «чистый» повторный ход при запросе на скидку, и
        # прогон харнесса. None (тесты, харнесс) — лимита нет.
        cost_guard: Any = None,
        # Часы хода. Момент берётся ОДИН РАЗ на ход и уходит в два места:
        # в блок «Сейчас:» системного промта и в сверку дат последнего
        # рубежа. Иначе рубеж сравнивал бы ответ с датой, которой модель не
        # видела, — и на стыке суток ловил бы несуществующее расхождение.
        now_fn: Any = None,
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
        self.cost_guard = cost_guard
        self.now_fn = now_fn or moscow_now
        # `self.kb`, а НЕ захваченный параметр `kb`: база знаний
        # перезагружается на лету, когда оператор правит цену из Telegram
        # (app/ops/menu_service.py), и обновление `agent_loop.kb` обязано
        # доезжать до новых исполнителей инструментов. С захваченным
        # параметром правка молча не влияла бы на расчёт до рестарта.
        self.executor_factory = executor_factory or (
            lambda dialog_id, state, concessions_blocked=False: ToolExecutor(
                self.kb, dialog_id, state, booking_provider=booking_provider,
                photo_provider=photo_provider,
                concessions_blocked=concessions_blocked,
                concessions_today_provider=concessions_today_provider,
                booking_sink=booking_sink,
                booking_notifier=booking_notifier,
                booking_handoff_notifier=booking_handoff_notifier,
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
        # Один момент на весь ход — см. now_fn в конструкторе.
        now = self.now_fn()
        system = build_system_prompt(self.kb, now)

        # Подсказка о зоне идёт в СОДЕРЖИМОЕ этого хода, а не в системный
        # промт: она меняется от сообщения к сообщению, а кешируемый блок
        # (справочник зон, cache_control) обязан оставаться неизменным байт в
        # байт — иначе кеш промахивается на каждом ходу. history для
        # персистентной истории при этом не трогаем: подсказка только в
        # запросе к модели этим ходом, не в том, что вызывающий код сохранит.
        turn_content = user_text
        # Зона из объявления — тоже «клиент сказал, о чём речь»: он пришёл с
        # карточки конкретной зоны. Берём ТОЛЬКО однозначно разобранную:
        # у «ambiguous» в подсказке перечислено несколько зон, и считать это
        # за известную зону значит считать наоборот.
        listing_zone: str = ""
        if item_id is not None:
            resolution = await resolve_listing(item_id, item_lookup, self.kb)
            hint = build_listing_hint(resolution, self.kb)
            if getattr(resolution, "status", "") == "resolved" and resolution.zone_id:
                zone = next(
                    (z for z in self.kb.catalog.zones if z.id == resolution.zone_id), None
                )
                listing_zone = zone.name if zone is not None else ""
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
        tool_trace: list[dict] = []
        # Числа копим из СЫРОГО ответа инструмента, а не из tool_trace:
        # тот обрезан до TOOL_TRACE_LIMIT, и сверка по нему считала бы
        # выдумкой всё, что не поместилось в лог.
        allowed_amounts: set[str] = set()
        quote_statuses: list[str] = []
        final_text = ""
        hit_limit = False
        tool_call_errors = 0

        # ПРИНУЖДЕНИЕ ИНСТРУМЕНТА — ТОЛЬКО НА ПЕРВОМ ВИТКЕ.
        #
        # DeepSeek зовёт инструменты примерно в половине ходов (замеры — в
        # докстринге app/agent/tool_forcing.py). Промт это не лечит: его
        # усиливали дважды. Помогает адресный tool_choice: 29 ходов из 35
        # оперлись на инструмент против 3 из 35 без него.
        #
        # ЭТО НЕ ГАРАНТИЯ, и рассчитывать на неё нельзя: на одном из случаев
        # тот же замер дал 0 из 5, а через час — 5 из 5. Клиента защищает
        # последний рубеж ниже, а принуждение лишь даёт рубежу что пропускать.
        #
        # Держать принуждение на ВСЕХ витках нельзя: модель обязана вызвать
        # инструмент снова и снова и никогда не дойдёт до текста ответа. На
        # первом витке она обязана сходить в календарь, дальше — свободна.
        # КОНТЕКСТ ТОЛЬКО КЛИЕНТСКИЙ. Реплики агента сюда не идут намеренно:
        # его приветствие перечисляет все зоны разом («баня, купол,
        # гриль-домик или шатёр»), и если считать это за «зона названа», то
        # условие выполнялось бы всегда начиная со второго хода — то есть не
        # значило бы ничего.
        forcing_context = "\n".join(
            [m.get("content") or "" for m in history[-HISTORY_WINDOW:]
             if m.get("role") == "user"]
            + ([listing_zone] if listing_zone else [])
        )
        forced_tool = forced_tool_for(user_text, forcing_context)
        if forced_tool:
            logger.info(
                "tool_choice: принуждаю %s по сообщению клиента",
                forced_tool, extra={"dialog_id": dialog_id, "tool": forced_tool},
            )

        for iteration in range(MAX_TOOL_ITERATIONS):
            tool_choice = None
            if forced_tool and iteration == 0:
                # Именно адресная форма. {"type": "any"} DeepSeek принимает и
                # молча игнорирует — проверено, 1 вызов на 10 ходов. Имя при
                # этом для DeepSeek — выключатель, а не выбор: инструмент он
                # выберет свой. Имя всё равно осмысленное — как сильная
                # подсказка оно работает, а Anthropic исполняет его точно.
                tool_choice = {"type": "tool", "name": forced_tool}

            response = await self.provider.complete(
                model=self.dialog_model,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=TOOLS,
                messages=messages,
                tool_choice=tool_choice,
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
                # МУСОР ОТ СЛОЯ СОВМЕСТИМОСТИ. DeepSeek 2026-09-02 вернул
                # блок tool_use с именем "tool_calls" — такого инструмента мы
                # не объявляли. Ход из-за этого не падает и раньше:
                # ToolExecutor.run на незнакомое имя отдаёт {"error": ...},
                # модель получает ошибку и на следующем витке зовёт заново.
                # Не хватало только видимости — без строки в логе это
                # выглядит как «модель зачем-то ошиблась инструментом».
                name, arguments, block_id = normalize_tool_use(block)
                if name not in KNOWN_TOOL_NAMES:
                    logger.warning(
                        "провайдер вернул неизвестный инструмент %r — не наш, "
                        "отдаём модели ошибку и идём на следующий виток",
                        name,
                        extra={"dialog_id": dialog_id, "provider": self.provider.name},
                    )
                tool_calls.append(name)
                payload = await executor.run(name, arguments)

                # ЖУРНАЛ ВЫЗОВОВ. Разбор инцидента 2026-08-31 («сегодняшняя
                # дата уже прошла» на сегодняшнюю дату) пришлось вести по
                # косвенным уликам: в БД лежали только токены и стоимость, а
                # какие даты модель реально передала в create_booking —
                # нигде. Имя, аргументы и результат нужны целиком: без
                # аргументов не видно, ЧТО спросили, без результата — что
                # ответил код, а расходятся обычно именно они.
                record = {
                    "tool": name,
                    "arguments": _loggable(arguments),
                    "result": _loggable(payload),
                }
                tool_trace.append(record)
                logger.info(
                    "tool %s(%s) -> %s",
                    name,
                    json.dumps(record["arguments"], ensure_ascii=False)[:TOOL_TRACE_LIMIT],
                    json.dumps(record["result"], ensure_ascii=False)[:TOOL_TRACE_LIMIT],
                    extra={"dialog_id": dialog_id, "tool": name},
                )

                allowed_amounts |= amounts_in_payload(payload)

                if payload.get("error"):
                    tool_call_errors += 1
                if name == "calculate_price" and "status" in payload:
                    quote_statuses.append(payload["status"])
                results.append(tool_result_block(block_id, payload))
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
            # Журнал вызовов инструментов — рядом с расходом, в том же
            # llm_meta: он и так едет в БД с каждым исходящим, а отдельная
            # таблица ради разбора инцидентов ещё и рассинхронизируется с
            # сообщением, к которому относится.
            "tool_trace": tool_trace,
        }

        # Расход считается ДО ветки последнего рубежа: этот ход уже стоил
        # денег, даже если его текст клиенту не уйдёт. Предохранитель
        # оплаченное не возвращает — он ограничивает трату, а не выдачу.
        if self.cost_guard is not None:
            try:
                self.cost_guard.add(Decimal(llm_meta["cost_rub"]))
            except Exception:
                # Предохранитель не должен ронять ход клиента. Не сработал —
                # это видно в логе и в /admin/costs, а разговор продолжается.
                logger.exception("cost guard failed", extra={"dialog_id": dialog_id})

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
                tool_amounts=allowed_amounts,
                granted_offer_templates=list(executor.granted_offer_templates),
                hit_iteration_limit=hit_limit,
                llm_meta=llm_meta,
                tool_call_errors=tool_call_errors,
                concession_state=getattr(executor, "state", None),
                concession_events=getattr(executor, "concession_events", []),
            )

        # Тот же инвариант, второй вопрос к нему: вызов был, но сумма в
        # тексте не из его ответа. Разбор — у invented_amounts выше.
        if final_text and tool_calls:
            invented = invented_amounts(final_text, allowed_amounts)
            if invented:
                logger.error(
                    "guard rail: %s — %s",
                    AMOUNT_MISMATCH_VIOLATION, ", ".join(invented),
                    extra={
                        "dialog_id": dialog_id,
                        "provider": self.provider.name,
                        "withheld_text": final_text,
                        "tool_calls": tool_calls,
                    },
                )
                executor.escalated = True
                executor.escalation_reason = AMOUNT_MISMATCH_VIOLATION
                return TurnResult(
                    text="Секунду, уточню детали у менеджера и вернусь с ответом.",
                    escalated=True,
                    escalation_reason=AMOUNT_MISMATCH_VIOLATION,
                    classification=classification,
                    tool_calls=tool_calls,
                    quote_statuses=quote_statuses,
                    tool_amounts=allowed_amounts,
                    granted_offer_templates=list(executor.granted_offer_templates),
                    hit_iteration_limit=hit_limit,
                    llm_meta={**llm_meta, "guard_rail": AMOUNT_MISMATCH_VIOLATION,
                              "invented_amounts": invented,
                              "withheld_text": final_text},
                    tool_call_errors=tool_call_errors,
                    concession_state=getattr(executor, "state", None),
                    concession_events=getattr(executor, "concession_events", []),
                )

        # Второй последний рубеж: текст утверждает что-то про дату или
        # занятость, а инструмент, который даёт на это право, за ход не
        # вызывался. См. AVAILABILITY_TOOLS — там же разбор инцидента.
        #
        # НЕ эскалация, в отличие от ценового рубежа: спросить у клиента
        # число — обычная работа агента, звать ради этого человека значит
        # получить эскалацию на каждый вопрос «а когда свободно?». Ответ
        # уходит клиенту как обычный, разговор продолжается, а в логе
        # остаётся ERROR — по нему и видно, как часто модель пытается
        # сочинять календарь.
        guard_reason: Optional[str] = None
        if final_text:
            # `availability_claim`, а НЕ просто поиск слова: «уточню свободное
            # время» — обещание проверить, а не факт о календаре. Разбор — у
            # PROMISE_VERBS выше.
            if availability_claim(final_text) and not AVAILABILITY_TOOLS.intersection(
                tool_calls
            ):
                guard_reason = AVAILABILITY_GUARD_VIOLATION
            else:
                mismatch = date_contradicts_now(final_text, now)
                if mismatch:
                    guard_reason = f"{DATE_GUARD_VIOLATION}: {mismatch}"

        if guard_reason:
            repeats = guard_repeats(history)
            escalate = repeats >= len(AVAILABILITY_GUARD_REPLIES)
            reply = (
                AVAILABILITY_GUARD_HANDOFF if escalate
                else AVAILABILITY_GUARD_REPLIES[repeats]
            )
            logger.error(
                "guard rail: %s (подряд %d)%s",
                guard_reason, repeats + 1,
                " — передаю оператору" if escalate else "",
                extra={
                    "dialog_id": dialog_id,
                    "provider": self.provider.name,
                    "tool_calls": ",".join(tool_calls) or "(ни одного)",
                    "withheld_text": final_text[:TOOL_TRACE_LIMIT],
                },
            )
            return TurnResult(
                text=reply,
                # Эскалация только на третьем срабатывании. Раньше звать
                # человека нельзя: спросить у клиента число — обычная работа
                # агента, и эскалация на каждый вопрос про даты превратила бы
                # оператора в диспетчера.
                escalated=escalate or executor.escalated,
                escalation_reason=(
                    AVAILABILITY_GUARD_HANDOFF_REASON if escalate
                    else executor.escalation_reason
                ),
                classification=classification,
                tool_calls=tool_calls,
                quote_statuses=quote_statuses,
                tool_amounts=allowed_amounts,
                granted_offer_templates=list(executor.granted_offer_templates),
                hit_iteration_limit=hit_limit,
                llm_meta={**llm_meta, "guard_rail": guard_reason,
                          "guard_repeats": repeats + 1,
                          "withheld_text": final_text},
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
            # Именно ЗДЕСЬ это важнее всего: на успешном пути текст уходит
            # клиенту, и харнесс качества проверяет как раз его. Без этой
            # строки набор приезжал бы пустым, правило price_mismatch не
            # срабатывало бы никогда, а отчёт выглядел бы чистым.
            tool_amounts=allowed_amounts,
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
