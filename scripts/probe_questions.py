"""Живой замер: допрашивает ли агент клиента.

    python -m scripts.probe_questions                  # 5 повторов на случай
    python -m scripts.probe_questions --repeats 10
    python -m scripts.probe_questions --case complaint_verbatim

ЗАЧЕМ ОТДЕЛЬНЫЙ СКРИПТ, А НЕ ТЕСТ. Проверяется не наш код, а ПОВЕДЕНИЕ
чужой модели, и оно недетерминированно: один и тот же ход то укладывается в
один вопрос, то разворачивается в анкету. Такому месту нельзя стоять в
pytest — один провал из пяти повторов красил бы сборку в красный на ровном
месте. Тот же довод и то же устройство, что у scripts/probe_tool_forcing.py.

ЧТО ИМЕННО МЕРЯЕТСЯ. Заказчик после двух недель в проде: «бот заваливает
вопросами и теряет клиентов», броней через бота — ноль. Разложено на четыре
отдельно наблюдаемые величины:

  1. АНКЕТНОСТЬ ДО РУБЕЖА — доля ответов, где модель написала больше одного
     вопроса. Это поведение самой модели, и именно оно сравнимо с прошлым
     замером (9% на 2026-09-02, docs/quality/tool_forcing_probe.md).
  2. АНКЕТНОСТЬ ПОСЛЕ РУБЕЖА — то же самое в тексте, который реально уходит
     клиенту. Обязано быть НУЛЁМ: рубеж режет лишние вопросы
     (app/agent/one_question.py). Ненулевое значение здесь — не «модель
     плохо себя вела», а дыра в рубеже.
  3. ПЕРЕСПРОС ИЗВЕСТНОГО — доля ответов, где агент спрашивает то, что
     клиент уже назвал в этом же диалоге. Главная жалоба дословно: «клиент
     назвал дату и число гостей три сообщения назад».
  4. ПОВТОР СВОЕГО ЖЕ ВОПРОСА — доля ответов, где агент второй раз задаёт
     вопрос, который уже задавал (и ответа не получил).

Плюс пятая, обратная: НАЗВАЛ ЛИ ЦИФРУ там, где клиент спросил про цену.
«Не молчи про деньги» — требование из того же письма, и без него первые
четыре метрики можно было бы обнулить, отвечая односложно и ни о чём.

ДВУХ ПЛЕЧЕЙ ЗДЕСЬ НЕТ, в отличие от замера принуждения, и это не экономия.
Рубеж и подсказка со слотами встроены в ход и выключаются только правкой
кода; «плечо до» получается само — это метрика №1, посчитанная по
неурезанному тексту, который ход сохраняет в llm_meta целиком.

ЧТО НАСТОЯЩЕЕ. Модель, системный промт, объявления инструментов, петля
run_turn и все рубежи — как в проде. Не настоящий только YCLIENTS: вместо
него календарь-заглушка, у которой всё свободно с 13:00 (_FreeCalendar).

ПОЧЕМУ ЗАГЛУШКА, А НЕ «БЕЗ КАЛЕНДАРЯ». Первые два прогона шли без
booking_provider, и check_availability отвечал "unknown" — на что агент по
правилам честно передаёт вопрос о занятости человеку. Главный случай жалобы
(«есть свободное время и сколько стоит?» с карточки гриль-домика) из-за
этого в трёх ответах из пяти уходил к менеджеру и вообще не мерил то, ради
чего заведён. В проде календарь подключён, и «unknown» там — сбой, а не
норма. Боевой YCLIENTS ради статистики не дёргаем.

Стоимость: один ход — вызов классификатора плюс вызов диалоговой модели и
по вызову на каждый виток инструментов. Прогон по умолчанию (9 случаев × 5
повторов) обходится в десятки рублей.

Отчёт: docs/quality/questions_probe.md (+ .json рядом).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.agent.loop import PRICE_LIKE_WITHOUT_TOOL_CALL, AgentLoop
from app.agent.one_question import count_questions
from app.agent.providers.deepseek_provider import BASE_URL as DEEPSEEK_BASE_URL
from app.agent.providers.deepseek_provider import DeepSeekProvider
from app.agent.providers.factory import default_models_for
from app.agent.slots import asked_slots, extract_slots
from app.agent.listing_context import ItemZoneRow
from app.agent.tools import ToolExecutor
from app.booking.base import Availability, AvailabilityStatus, BookingResult
from app.kb.loader import load_catalog

ROOT = Path(__file__).resolve().parent.parent

# Слова, за которыми в каталоге стоит несколько зон сразу: три бани, три
# купола. Уточнить, какая именно, — не переспрос.
CATEGORY_WORDS = re.compile(r"(?i)^(?:бан|купол|сфер)")
REPORT_MD = ROOT / "docs" / "quality" / "questions_probe.md"
REPORT_JSON = ROOT / "docs" / "quality" / "questions_probe.json"


@dataclass(frozen=True)
class Case:
    """Один случай: что уже сказано, что спрашивает клиент, чего мы ждём."""

    id: str
    client_text: str
    why: str
    history: tuple[dict, ...] = ()
    # Ждём ли в ответе цифру про деньги. None — «неважно для этого случая».
    #
    # True СТАВИТСЯ ТОЛЬКО ТАМ, ГДЕ ЗАКАЗЧИК ЭТОГО ТРЕБУЕТ: известны зона и
    # день («если известна зона и день недели — цену за час можно называть
    # всегда»). Первый прогон 2026-09-18 ждал цифру и там, где зоны или дня
    # нет, и честный ответ «какая зона вас интересует?» считался провалом —
    # а назвать ставку там нечем: у бань и куполов она разная по будням и
    # выходным, у разных зон — разная вообще.
    expect_money: Optional[bool] = None
    # Зона объявления, с которого пришёл клиент (как `item_id` в проде).
    listing_zone_id: Optional[str] = None


# Случаи — из письма заказчика и из живых переписок. Каждый отвечает на
# отдельный вопрос, поэтому набор не сокращается «для скорости».
CASES: tuple[Case, ...] = (
    Case(
        id="complaint_verbatim",
        client_text="хочу арендовать 20 сентября, есть свободное время и сколько стоит?",
        why="ДОСЛОВНО из жалобы заказчика 2026-09-18, но БЕЗ объявления — "
            "как из профиля продавца. Зона неизвестна, значит ставку назвать "
            "нечем; верный ответ — один вопрос про зону, не про дату",
    ),
    Case(
        id="complaint_from_grill_listing",
        client_text="хочу арендовать 20 сентября, есть свободное время и сколько стоит?",
        why="Та же реплика с карточки гриль-домика — именно так она пришла "
            "в проде. Зона и дата известны: обязана прозвучать ставка",
        expect_money=True,
        listing_zone_id="grill_house",
    ),
    Case(
        id="date_and_guests_three_turns_back",
        client_text="а цена какая?",
        why="Главная форма жалобы: дата и число гостей названы тремя "
            "репликами раньше. Ни то, ни другое спрашивать заново нельзя",
        history=(
            {"role": "user", "content": "Здравствуйте, интересует гриль-домик на 20 сентября"},
            {"role": "assistant", "content": "Здравствуйте! Меня зовут Иришка. Сколько вас будет?"},
            {"role": "user", "content": "нас 6теро"},
            {"role": "assistant", "content": "Поняла, шестеро. На сколько часов планируете?"},
        ),
        expect_money=True,
    ),
    Case(
        id="price_of_a_bath_no_details",
        client_text="сколько стоит баня?",
        why="Зона названа, дня нет. По правилу заказчика цифра обязательна, "
            "когда известны зона И день; здесь дня нет, а будни и выходные "
            "у бань стоят по-разному. Смотрим, что агент делает без него",
    ),
    Case(
        id="only_duration_missing",
        client_text="а сколько это будет стоить?",
        why="Не хватает ровно длительности. Требование дословно: «назови "
            "цену за час и минимум, а не молчи про деньги»",
        history=(
            {"role": "user", "content": "интересует баня на 20 сентября, нас шестеро"},
            {"role": "assistant", "content": "Здравствуйте! У нас три бани на выбор."},
        ),
        expect_money=True,
    ),
    Case(
        id="grill_house_on_a_weekend",
        client_text="гриль-домик в субботу сколько будет стоить?",
        why="Расчёт возвращает invalid (пакет «весь день» только пн-чт). До "
            "правки клиент не получал НИ ОДНОЙ цифры — только объяснение "
            "про пакет",
        expect_money=True,
    ),
    Case(
        id="client_ignored_the_question",
        client_text="а у вас есть где детям поиграть?",
        why="Клиент не ответил на вопрос про гостей и спросил своё. "
            "Повторять тот же вопрос нельзя — надо ответить на его",
        history=(
            {"role": "user", "content": "хочу баню на завтра"},
            {"role": "assistant", "content": "Здравствуйте! Сколько вас будет?"},
        ),
    ),
    Case(
        id="everything_is_known",
        client_text="ну что, подойдёт?",
        why="Клиент назвал всё: зону, дату, время, длительность, гостей. "
            "Любой уточняющий вопрос здесь — переспрос известного. Цифра "
            "обязательна: зона и день известны",
        history=(
            {"role": "user", "content": "баня Русский стиль, 20 сентября, с 13 до 17, нас шестеро"},
            {"role": "assistant", "content": "Здравствуйте! Секунду, посмотрю."},
        ),
        expect_money=True,
    ),
    Case(
        id="bare_price_question",
        client_text="а цена какая?",
        why="Ни зоны, ни даты — тот самый случай, который в замере "
            "2026-09-02 дал 18% анкетных ответов при принуждении "
            "инструмента. Один вопрос, не три",
    ),
)


@dataclass
class Attempt:
    """Один ход: что уехало клиенту и что модель написала до рубежа."""

    text: str = ""
    untrimmed: str = ""
    questions_before: int = 0
    questions_after: int = 0
    asked_in_reply: list[str] = field(default_factory=list)
    # Спросил про то, что клиент уже называл.
    reasked_known: list[str] = field(default_factory=list)
    # Спросил второй раз то, о чём уже спрашивал сам.
    repeated_own: list[str] = field(default_factory=list)
    names_money: bool = False
    tools: list[str] = field(default_factory=list)
    guard_rail: Optional[str] = None
    # Что модель написала, когда рубеж подменил ответ. Без этого поля
    # срабатывание рубежа в отчёте не разобрать: видна только подстановка.
    withheld_text: Optional[str] = None
    question_guard: Optional[str] = None
    escalated: bool = False
    error: Optional[str] = None
    latency_ms: float = 0.0


class _FreeCalendar:
    """Календарь, в котором всё свободно с 13:00. Брони не ставит."""

    SLOTS = ("13:00", "14:00", "15:00", "16:00", "17:00", "18:00")

    async def get_services(self):
        return []

    async def check_availability(self, zone_id, date, start_time=None, hours=None):
        return Availability(AvailabilityStatus.FREE, free_slots=self.SLOTS)

    async def get_free_slots(self, zone_id, date):
        return Availability(AvailabilityStatus.FREE, free_slots=self.SLOTS)

    async def create_booking(self, request):
        return BookingResult(success=False, error="замер: брони не ставятся")

    async def cancel_booking(self, booking_id):
        return BookingResult(success=False, error="замер")

    async def create_payment_link(self, booking_id, amount):
        return None


class _ListingStub:
    """Объявление, с которого пришёл клиент, — без похода в базу."""

    def __init__(self, zone_id: str):
        self.zone_id = zone_id

    async def get(self, item_id: str) -> ItemZoneRow:
        return ItemZoneRow(zone_id=self.zone_id)


async def run_attempt(agent: AgentLoop, kb: Any, case: Case) -> Attempt:
    executor = ToolExecutor(kb, f"probe-{case.id}", booking_provider=_FreeCalendar())
    agent.executor_factory = lambda did, state, _ex=executor: _ex

    listing: dict[str, Any] = {}
    if case.listing_zone_id:
        listing = {"item_id": f"probe-item-{case.listing_zone_id}",
                   "item_lookup": _ListingStub(case.listing_zone_id)}

    started = time.monotonic()
    try:
        result = await agent.run_turn(
            f"probe-{case.id}", list(case.history), case.client_text, **listing
        )
    except Exception as exc:  # noqa: BLE001
        return Attempt(error=f"{type(exc).__name__}: {exc}",
                       latency_ms=(time.monotonic() - started) * 1000)

    text = result.text or ""
    # Текст ДО рубежа одного вопроса. Его сохраняет сам ход — брать его
    # откуда-то ещё значило бы мерить не то, что реально произошло.
    untrimmed = result.llm_meta.get("untrimmed_text") or text

    # Слоты считаются ровно теми же функциями, что и в проде: замер, у
    # которого своё представление о «клиент уже назвал дату», мерил бы
    # собственную фантазию, а не поведение агента.
    listing_name = ""
    if case.listing_zone_id:
        zone = next((z for z in kb.catalog.zones if z.id == case.listing_zone_id), None)
        listing_name = zone.name if zone is not None else ""
    known = extract_slots(list(case.history), case.client_text, listing_zone=listing_name)
    previously_asked = set(asked_slots(list(case.history)))
    asked_now = set(asked_slots([{"role": "assistant", "content": untrimmed}]))

    return Attempt(
        text=text,
        untrimmed=untrimmed,
        questions_before=count_questions(untrimmed),
        questions_after=count_questions(text),
        asked_in_reply=sorted(asked_now),
        reasked_known=sorted(
            slot for slot in asked_now & set(known.known())
            # «Какую баню — Русский стиль, Гараж или Рыцарскую?» после «хочу
            # баню» — выбор внутри категории, а не переспрос: бань три, и
            # промт прямо требует этот вопрос. Первый прогон считал его
            # переспросом зоны.
            if not (slot == "zone" and not known.zone_from_listing
                    and CATEGORY_WORDS.match(known.zone))
        ),
        repeated_own=sorted((asked_now & previously_asked) - set(known.known())),
        names_money=bool(PRICE_LIKE_WITHOUT_TOOL_CALL.search(text)),
        tools=list(result.tool_calls),
        guard_rail=result.llm_meta.get("guard_rail"),
        withheld_text=result.llm_meta.get("withheld_text"),
        question_guard=result.llm_meta.get("question_guard"),
        escalated=result.escalated,
        latency_ms=(time.monotonic() - started) * 1000,
    )


async def probe(repeats: int, only: Optional[str], provider_name: str) -> dict:
    from app.config import get_settings

    settings = get_settings()

    if provider_name == "deepseek":
        api_key = (settings.deepseek_api_key.get_secret_value()
                   or os.environ.get("DEEPSEEK_API_KEY", ""))
    else:
        api_key = (settings.anthropic_api_key.get_secret_value()
                   or os.environ.get("ANTHROPIC_API_KEY", ""))
    if not api_key:
        raise SystemExit(
            f"нет ключа для провайдера {provider_name} — замер живой, "
            "заглушка здесь бессмысленна: она задаёт ровно столько вопросов, "
            "сколько мы её научили, и меряла бы саму себя"
        )

    kb = load_catalog()
    dialog_model, classifier_model = default_models_for(provider_name)
    if provider_name == "deepseek":
        client: Any = DeepSeekProvider(
            api_key=api_key, base_url=DEEPSEEK_BASE_URL,
            enable_thinking=settings.deepseek_enable_thinking,
        )
    else:
        from anthropic import AsyncAnthropic

        from app.agent.providers.anthropic_provider import AnthropicProvider

        client = AnthropicProvider(client=AsyncAnthropic(api_key=api_key))

    agent = AgentLoop(client, kb, dialog_model=dialog_model,
                      classifier_model=classifier_model)

    cases = [c for c in CASES if only is None or c.id == only]
    if not cases:
        raise SystemExit(f"нет случая с id {only!r}; есть: "
                         + ", ".join(c.id for c in CASES))

    report: dict = {"provider": provider_name, "model": dialog_model,
                    "repeats": repeats, "cases": []}

    for case in cases:
        entry = {"id": case.id, "client_text": case.client_text, "why": case.why,
                 "expect_money": case.expect_money, "attempts": []}
        for _ in range(repeats):
            attempt = await run_attempt(agent, kb, case)
            entry["attempts"].append(attempt.__dict__)
            print(
                f"  [{case.id}] вопросов {attempt.questions_before}"
                f"->{attempt.questions_after}"
                f"{' переспрос: ' + ','.join(attempt.reasked_known) if attempt.reasked_known else ''}"
                f"{' деньги' if attempt.names_money else ''}",
                file=sys.stderr,
            )
        report["cases"].append(entry)

    return report


# --------------------------------------------------------------------------
# Счёт
# --------------------------------------------------------------------------

def _ok(attempts: list[dict]) -> list[dict]:
    """Только состоявшиеся попытки: упавший ход не «ответ без вопросов»."""
    return [a for a in attempts if not a.get("error")]


def _rate(part: int, whole: int) -> str:
    return f"{part}/{whole} = {part / whole:.0%}" if whole else "нет данных"


def _many_questions_before(attempts: list[dict]) -> int:
    return sum(1 for a in _ok(attempts) if a["questions_before"] > 1)


def _many_questions_after(attempts: list[dict]) -> int:
    return sum(1 for a in _ok(attempts) if a["questions_after"] > 1)


def _reasked_known(attempts: list[dict]) -> int:
    return sum(1 for a in _ok(attempts) if a["reasked_known"])


def _repeated_own(attempts: list[dict]) -> int:
    return sum(1 for a in _ok(attempts) if a["repeated_own"])


def _average_questions(attempts: list[dict]) -> float:
    done = _ok(attempts)
    return statistics.mean([a["questions_after"] for a in done]) if done else 0.0


def _named_money(attempts: list[dict]) -> int:
    return sum(1 for a in _ok(attempts) if a["names_money"])


def render(report: dict) -> str:
    n = report["repeats"]
    everything = [a for case in report["cases"] for a in case["attempts"]]
    money_cases = [
        a for case in report["cases"] if case["expect_money"]
        for a in case["attempts"]
    ]

    lines = [
        "# Живой замер: допрашивает ли агент клиента",
        "",
        f"Провайдер **{report['provider']}**, модель `{report['model']}`, "
        f"по **{n}** повторов на случай.",
        "",
        "Метрики и почему они именно такие — в докстринге "
        "`scripts/probe_questions.py`.",
        "",
        "## Итог",
        "",
        "| величина | значение |",
        "|---|---|",
        f"| среднее число вопросов в ответе | "
        f"**{_average_questions(everything):.2f}** |",
        f"| больше одного вопроса ДО рубежа (поведение модели) | "
        f"**{_rate(_many_questions_before(everything), len(_ok(everything)))}** |",
        f"| больше одного вопроса ПОСЛЕ рубежа (уехало клиенту) | "
        f"**{_rate(_many_questions_after(everything), len(_ok(everything)))}** |",
        f"| переспрос того, что клиент уже назвал | "
        f"**{_rate(_reasked_known(everything), len(_ok(everything)))}** |",
        f"| повтор своего же вопроса | "
        f"**{_rate(_repeated_own(everything), len(_ok(everything)))}** |",
        f"| названа цифра там, где клиент спросил про цену | "
        f"**{_rate(_named_money(money_cases), len(_ok(money_cases)))}** |",
        "",
        "Прошлый замер (2026-09-02, docs/quality/tool_forcing_probe.md) давал "
        "9% ответов больше чем с одним вопросом. Сравнима с ним строка «ДО "
        "рубежа»: она про поведение модели. Строка «ПОСЛЕ рубежа» — про то, "
        "что видит клиент, и она обязана быть нулём.",
        "",
    ]

    broken = [a for a in everything if a.get("error")]
    if broken:
        lines += [
            f"> ⚠️ Упавших попыток: **{len(broken)}** из {len(everything)}. "
            "Цифры выше настолько же недостоверны.",
            "",
        ]

    lines += ["## По случаям", ""]
    for case in report["cases"]:
        attempts = case["attempts"]
        done = _ok(attempts)
        lines += [
            f"### `{case['id']}`",
            "",
            f"Клиент: «{case['client_text']}»",
            "",
            case["why"] + ".",
            "",
            f"- вопросов в ответе: в среднем **{_average_questions(attempts):.2f}**",
            f"- больше одного до рубежа: {_rate(_many_questions_before(attempts), len(done))}",
            f"- больше одного после рубежа: {_rate(_many_questions_after(attempts), len(done))}",
            f"- переспрос известного: {_rate(_reasked_known(attempts), len(done))}",
            f"- повтор своего вопроса: {_rate(_repeated_own(attempts), len(done))}",
        ]
        if case["expect_money"]:
            lines.append(f"- названа цифра: {_rate(_named_money(attempts), len(done))}")
        lines.append("")
        for i, attempt in enumerate(attempts, 1):
            if attempt.get("error"):
                lines.append(f"{i}. ⚠️ {attempt['error']}")
                continue
            mark = " ✂️" if attempt.get("question_guard") else ""
            lines.append(f"{i}.{mark} {(attempt['text'] or '—')[:300]}")
            if attempt.get("guard_rail"):
                lines.append(
                    f"   - рубеж «{attempt['guard_rail'][:60]}», модель писала: "
                    f"{(attempt.get('withheld_text') or '')[:300]}"
                )
            if attempt.get("question_guard"):
                lines.append(f"   - до рубежа: {attempt['untrimmed'][:300]}")
            if attempt["reasked_known"]:
                lines.append(
                    "   - ПЕРЕСПРОС уже названного: "
                    + ", ".join(attempt["reasked_known"])
                )
        lines.append("")

    return "\n".join(lines) + "\n"


async def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=5,
                        help="повторов на случай (по умолчанию 5)")
    parser.add_argument("--case", help="прогнать только один случай по id")
    parser.add_argument("--provider", choices=["deepseek", "anthropic"],
                        default="deepseek")
    parser.add_argument("--out", type=Path,
                        help="куда положить отчёт (без расширения)")
    parser.add_argument("--merge", nargs="+", type=Path,
                        help="сшить готовые json-куски в один отчёт и выйти")
    args = parser.parse_args()

    if args.merge:
        pieces = [json.loads(path.read_text(encoding="utf-8")) for path in args.merge]
        report = dict(pieces[0])
        order = {case.id: i for i, case in enumerate(CASES)}
        report["cases"] = sorted(
            [c for piece in pieces for c in piece["cases"]],
            key=lambda c: order.get(c["id"], len(order)),
        )
    else:
        report = await probe(args.repeats, args.case, args.provider)

    json_path = args.out.with_suffix(".json") if args.out else REPORT_JSON
    md_path = args.out.with_suffix(".md") if args.out else REPORT_MD
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    md_path.write_text(render(report), encoding="utf-8")

    everything = [a for case in report["cases"] for a in case["attempts"]]
    done = _ok(everything)
    money_cases = [
        a for case in report["cases"] if case["expect_money"] for a in case["attempts"]
    ]
    print(f"\nПровайдер {report['provider']}, модель {report['model']}, "
          f"{report['repeats']} повторов на случай\n")
    for case in report["cases"]:
        attempts = case["attempts"]
        print(f"{case['id']:<32} вопросов в среднем "
              f"{_average_questions(attempts):.2f}   "
              f"анкет до/после {_many_questions_before(attempts)}/"
              f"{_many_questions_after(attempts)}   "
              f"переспрос {_reasked_known(attempts)}")
    print(f"\nсреднее число вопросов в ответе: {_average_questions(everything):.2f}")
    print(f"больше одного вопроса ДО рубежа:   {_rate(_many_questions_before(everything), len(done))}")
    print(f"больше одного вопроса ПОСЛЕ:       {_rate(_many_questions_after(everything), len(done))}")
    print(f"переспрос уже названного:          {_rate(_reasked_known(everything), len(done))}")
    print(f"повтор своего же вопроса:          {_rate(_repeated_own(everything), len(done))}")
    print(f"цифра там, где спросили цену:      {_rate(_named_money(money_cases), len(_ok(money_cases)))}")
    broken = [a for a in everything if a.get("error")]
    if broken:
        print(f"\n⚠️ упавших попыток: {len(broken)} — цифры настолько же недостоверны")
    print(f"\nОтчёт: {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
