"""Живой замер: зовёт ли DeepSeek инструмент — с принуждением и без.

    python -m scripts.probe_tool_forcing                 # 5 повторов на случай
    python -m scripts.probe_tool_forcing --repeats 10
    python -m scripts.probe_tool_forcing --case price_bare

ЗАЧЕМ ОТДЕЛЬНЫЙ СКРИПТ, А НЕ ТЕСТ. Проверяется не наш код, а ПОВЕДЕНИЕ
чужой модели, и оно недетерминированно: у DeepSeek вызов инструмента —
монетка, поэтому единственный осмысленный ответ здесь «сколько из N», а не
«прошло/не прошло». Такому месту нельзя стоять в pytest: один провал из
пяти повторов красил бы сборку в красный на ровном месте, а зелёная сборка
переставала бы что-либо значить.

ПОЧЕМУ ДВА ПЛЕЧА. Одно плечо ничего не доказывает: «5 из 5 с принуждением»
без контрольного замера не отличить от «эта модель сегодня в настроении».
Контрольное плечо — тот же самый ход, но `forced_tool_for` заглушён в None,
то есть ровно прод до этой правки. Разница между плечами и есть измеряемая
величина.

ЧТО ЗДЕСЬ НАСТОЯЩЕЕ. Модель, системный промт, объявления инструментов,
петля `AgentLoop.run_turn` и последний рубеж — всё как в проде. Не настоящий
только YCLIENTS: `ToolExecutor` создаётся без `booking_provider`, поэтому
`check_availability` возвращает `status: "unknown"`. Для замера это не важно
— считается ФАКТ ВЫЗОВА, а не то, что ответил календарь; и лучше так, чем
дёргать боевой календарь ради статистики.

Стоимость: один ход — два вызова (классификатор на flash + диалог на pro),
плюс по вызову на каждый виток инструментов. Прогон по умолчанию (8 случаев
× 5 повторов × 2 плеча) обходится в десятки рублей, не в тысячи.

Отчёт: docs/quality/tool_forcing_probe.md (+ .json рядом).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import app.agent.loop as loop_module
from app.agent.loop import AVAILABILITY_TOOLS, AgentLoop
from app.agent.providers.base import LLMProvider
from app.agent.providers.deepseek_provider import BASE_URL as DEEPSEEK_BASE_URL
from app.agent.providers.deepseek_provider import DeepSeekProvider
from app.agent.providers.factory import default_models_for
from app.agent.tools import ToolExecutor
from app.kb.loader import load_catalog

ROOT = Path(__file__).resolve().parent.parent
REPORT_MD = ROOT / "docs" / "quality" / "tool_forcing_probe.md"
REPORT_JSON = ROOT / "docs" / "quality" / "tool_forcing_probe.json"

# Инструменты, вызов которых даёт право говорить о занятости ИЛИ о цене.
# Ровно те же множества, по которым судит последний рубеж, — контрольное
# плечо меряется тем же мерилом, что и прод.
GROUNDING_TOOLS = AVAILABILITY_TOOLS | {"calculate_price"}


@dataclass(frozen=True)
class Case:
    """Один случай: что сказал клиент и что обязано произойти."""

    id: str
    client_text: str
    expected_tool: Optional[str]
    why: str
    history: tuple[dict, ...] = ()


# Случаи — из требования заказчика и из двух живых инцидентов. Каждый
# отвечает на отдельный вопрос, поэтому набор не сокращается «для скорости».
CASES: tuple[Case, ...] = (
    Case(
        id="incident_window_today",
        client_text="на сегодня есть окошко 4 часа , нас 6теро",
        expected_tool="check_availability",
        why="дословный текст клиента из инцидента 2026-09-01: дата, время и "
            "вопрос про занятость в одном сообщении",
    ),
    Case(
        id="incident_second_turn",
        client_text="сегодня 16 00",
        expected_tool="check_availability",
        why="ВТОРОЙ ход того же инцидента: слова про занятость в самом "
            "сообщении нет, признак остался в предыдущей реплике",
        history=(
            {"role": "user", "content": "на сегодня есть окошко 4 часа , нас 6теро"},
            {"role": "assistant", "content": "Подскажите, пожалуйста, на какое "
                                             "число и на сколько гостей вы рассчитываете?"},
        ),
    ),
    Case(
        id="window_relative_day",
        client_text="Есть окошко сегодня?",
        expected_tool="resolve_date",
        why="относительная дата без времени — сначала разобрать «сегодня» в "
            "число, считать это в уме модели нельзя (инциденты 31.08 и 01.09)",
    ),
    Case(
        id="nearest_available",
        client_text="когда ближайшее свободное?",
        expected_tool="find_next_available",
        why="числа клиент не называет — спрашивать его у клиента незачем",
    ),
    Case(
        id="concrete_booking",
        client_text="можно записаться на 5 сентября в 18:00?",
        expected_tool="check_availability",
        why="конкретная дата плюс слово про запись",
    ),
    Case(
        id="price_with_hours",
        client_text="Сколько стоит баня на 4 часа?",
        expected_tool="calculate_price",
        why="цена: без вызова инструмента её нельзя называть вообще",
    ),
    Case(
        id="price_bare",
        client_text="а цена какая?",
        expected_tool=None,
        why="цена без зоны и без даты — принуждать НЕЧЕГО. calculate_price "
            "требует zone_id и date; принуждение здесь заставляло модель "
            "выдумать оба поля, получить needs_input и спросить у клиента всё "
            "разом. Замер 2026-09-02: доля ответов больше чем с одним вопросом "
            "18% против 2% без принуждения",
    ),
    Case(
        id="price_after_zone",
        client_text="а цена какая?",
        expected_tool="calculate_price",
        why="тот же вопрос, но зона уже названа ходом раньше — вызов снова "
            "осмыслен. Именно этот случай и делает сужение безопасным: "
            "разговор почти всегда начинается с зоны",
        history=(
            {"role": "user", "content": "Здравствуйте, интересует баня"},
            {"role": "assistant", "content": "Здравствуйте! На какую дату планируете?"},
        ),
    ),
    Case(
        id="callback_tomorrow",
        client_text="завтра перезвоню",
        expected_tool=None,
        why="требование заказчика дословно: дата есть, вопроса про запись нет "
            "— календарь дёргать не за что",
    ),
)


@dataclass
class Attempt:
    """Один ход одного плеча."""

    # Что мы ПОПРОСИЛИ (tool_choice первого витка) и что модель РЕАЛЬНО
    # позвала первым. Это разные вещи, и различать их обязательно: провайдер
    # волен исполнить просьбу как «позови хоть что-нибудь».
    asked_tool: Optional[str] = None
    first_tool: Optional[str] = None
    tools: list[str] = field(default_factory=list)
    tool_args: dict = field(default_factory=dict)
    text: str = ""
    guard_rail: Optional[str] = None
    escalated: bool = False
    error: Optional[str] = None
    latency_ms: float = 0.0


class RecordingProvider(LLMProvider):
    """Обёртка провайдера, которая запоминает отправленный `tool_choice`.

    Без неё замер отвечает только на вопрос «позвала ли модель инструмент», а
    главный вопрос другой: позвала ли она ИМЕННО ТОТ, который у неё просили.
    Провайдер вправе понять просьбу как «позови хоть что-нибудь», и по одному
    имени в ответе этого не отличить от точного исполнения.
    """

    def __init__(self, inner: LLMProvider):
        self.inner = inner
        # LLMProvider объявляет `name` полем класса, не свойством, — берём
        # имя внутреннего провайдера, чтобы логи хода не врали про DeepSeek.
        self.name = inner.name
        self.tool_choices: list[Optional[dict]] = []

    def estimate_cost(self, *args, **kwargs):
        return self.inner.estimate_cost(*args, **kwargs)

    @property
    def supports_prompt_caching(self) -> bool:
        return self.inner.supports_prompt_caching

    async def complete(self, **kwargs):
        # Классификатор идёт без инструментов — его витки в счёт не берём.
        if kwargs.get("tools"):
            self.tool_choices.append(kwargs.get("tool_choice"))
        return await self.inner.complete(**kwargs)


class RecordingExecutor(ToolExecutor):
    """Тот же исполнитель, но запоминает аргументы вызовов.

    `TurnResult.tool_calls` хранит одни имена, а в случае `price_bare` весь
    вопрос как раз в аргументах: что модель подставит в обязательную `date`,
    когда клиент даты не называл.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seen: list[tuple[str, dict]] = []

    async def run(self, name: str, args: dict) -> dict:
        self.seen.append((name, dict(args)))
        return await super().run(name, args)


async def run_attempt(agent: AgentLoop, kb: Any, case: Case) -> Attempt:
    executor = RecordingExecutor(kb, f"probe-{case.id}")
    agent.executor_factory = lambda did, state, _ex=executor: _ex
    provider: RecordingProvider = agent.provider
    provider.tool_choices.clear()

    started = time.monotonic()
    try:
        result = await agent.run_turn(
            f"probe-{case.id}", list(case.history), case.client_text
        )
    except Exception as exc:  # noqa: BLE001
        return Attempt(error=f"{type(exc).__name__}: {exc}",
                       latency_ms=(time.monotonic() - started) * 1000)

    args_by_tool = {}
    for name, args in executor.seen:
        args_by_tool.setdefault(name, args)

    first_choice = provider.tool_choices[0] if provider.tool_choices else None

    return Attempt(
        asked_tool=(first_choice or {}).get("name"),
        first_tool=result.tool_calls[0] if result.tool_calls else None,
        tools=list(result.tool_calls),
        tool_args=args_by_tool,
        text=result.text,
        guard_rail=result.llm_meta.get("guard_rail"),
        escalated=result.escalated,
        latency_ms=(time.monotonic() - started) * 1000,
    )


async def probe(repeats: int, only: Optional[str], provider_name: str,
                control: bool = True) -> dict:
    # Ключ — из тех же настроек, что и у прода (они читают .env), а не
    # только из окружения: иначе замер «живой модели» молча превращался бы в
    # отказ на машине, где .env лежит рядом, но в оболочку не экспортирован.
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
            "заглушка здесь бессмысленна: она вызывает инструменты ровно "
            "столько раз, сколько мы её научили, и меряла бы саму себя"
        )

    kb = load_catalog()
    dialog_model, classifier_model = default_models_for(provider_name)
    if provider_name == "deepseek":
        client: Any = DeepSeekProvider(
            api_key=api_key, base_url=DEEPSEEK_BASE_URL,
            # Как в проде: thinking выключен. Включённый менял бы и задержку,
            # и стоимость, и, возможно, саму охоту звать инструменты.
            enable_thinking=settings.deepseek_enable_thinking,
        )
    else:
        from anthropic import AsyncAnthropic

        from app.agent.providers.anthropic_provider import AnthropicProvider

        client = AnthropicProvider(client=AsyncAnthropic(api_key=api_key))

    agent = AgentLoop(RecordingProvider(client), kb, dialog_model=dialog_model,
                      classifier_model=classifier_model)

    cases = [c for c in CASES if only is None or c.id == only]
    if not cases:
        raise SystemExit(f"нет случая с id {only!r}; есть: "
                         + ", ".join(c.id for c in CASES))

    report: dict = {"provider": provider_name, "model": dialog_model,
                    "repeats": repeats, "cases": []}

    original = loop_module.forced_tool_for
    for case in cases:
        entry = {"id": case.id, "client_text": case.client_text,
                 "expected_tool": case.expected_tool, "why": case.why,
                 "forced": [], "control": []}

        # Плечо «как в проде сейчас».
        for _ in range(repeats):
            attempt = await run_attempt(agent, kb, case)
            entry["forced"].append(attempt.__dict__)
            print(f"  [{case.id}] принуждение: {attempt.tools or '—'}", file=sys.stderr)

        # Контрольное плечо: тот же ход без принуждения. Заглушаем имя ровно
        # там, где loop.py его читает, — это и есть прод до правки.
        if control:
            loop_module.forced_tool_for = lambda _text: None
            try:
                for _ in range(repeats):
                    attempt = await run_attempt(agent, kb, case)
                    entry["control"].append(attempt.__dict__)
                    print(f"  [{case.id}] контроль:   {attempt.tools or '—'}", file=sys.stderr)
            finally:
                loop_module.forced_tool_for = original

        report["cases"].append(entry)

    return report


def _hits(attempts: list[dict], expected: Optional[str]) -> int:
    """Сколько попыток закончились ПРАВИЛЬНЫМ вызовом.

    Для случая «принуждать нечего» правильный исход обратный: не полезть в
    календарь и не считать цену без повода.
    """
    if expected is None:
        return sum(1 for a in attempts if not GROUNDING_TOOLS.intersection(a["tools"]))
    return sum(1 for a in attempts if expected in a["tools"])


def _obeyed_name(attempts: list[dict]) -> int:
    """Сколько раз модель позвала ИМЕННО тот инструмент, который просили.

    Отдельно от `_hits`, потому что «позвала хоть что-то» и «послушалась
    имени» — разные утверждения, и провайдер может исполнять только первое.
    """
    return sum(1 for a in attempts
               if a.get("asked_tool") and a.get("first_tool") == a["asked_tool"])


def _called_anything(attempts: list[dict]) -> int:
    """Сколько раз вызов инструмента вообще состоялся."""
    return sum(1 for a in attempts if a["tools"])


def _grounded(attempts: list[dict]) -> int:
    """Сколько попыток вообще опёрлись хоть на какой-то инструмент из тех,
    что дают право говорить о занятости или цене. Для контрольного плеча это
    честнее, чем требовать конкретное имя: модель без принуждения могла зайти
    с другой стороны и всё равно не выдумать ответ."""
    return sum(1 for a in attempts if GROUNDING_TOOLS.intersection(a["tools"]))


def render(report: dict) -> str:
    n = report["repeats"]
    lines = [
        "# Живой замер принуждения инструмента",
        "",
        f"Провайдер: **{report['provider']}**, модель **{report['model']}**, "
        f"по **{n}** повторов на случай в каждом плече.",
        "",
        "«Принуждение» — прод после правки: `tool_choice {\"type\": \"tool\", "
        "\"name\": ...}` на первом витке. «Контроль» — тот же ход с заглушённым "
        "`forced_tool_for`, то есть прод до неё. Одно плечо без другого ничего "
        "не доказывает: у DeepSeek вызов инструмента недетерминирован.",
        "",
        "Столбец «позвал именно его» читать отдельно: провайдер вправе "
        "понять просьбу как «позови хоть что-нибудь», и тогда предыдущий "
        "столбец полон, а этот пуст. У последнего случая принуждения нет "
        "вовсе, и успех там — пустой столбец «позвал хоть что-то».",
        "",
        "ЦИФРЫ — ЭТО ОДИН ПРОГОН В ОДИН МОМЕНТ, а не свойство модели. "
        "Поведение чужого слоя совместимости меняется во времени: 2026-09-02 "
        "случай `window_relative_day` дал 0 вызовов из 5, а он же через час — "
        "5 из 5, на том же коде и том же тексте. Поэтому принуждение не "
        "защита, а способ дать последнему рубежу что пропускать; защита — "
        "сам рубеж.",
        "",
        "| случай | просим | позвал хоть что-то | позвал именно его | "
        "без принуждения (контроль) |",
        "|---|---|---|---|---|",
    ]
    for case in report["cases"]:
        # То, что РЕАЛЬНО ушло в tool_choice. Задуманное и отправленное
        # расходятся, когда решение зависит от контекста разговора, и
        # печатать задуманное значило бы отчитываться о намерении.
        asked = {a.get("asked_tool") for a in case["forced"]} - {None}
        expected = ", ".join(sorted(asked)) if asked else "_ничего_"
        lines.append(
            f"| `{case['id']}` | {expected} | "
            f"{_called_anything(case['forced'])}/{n} | "
            + (f"{_obeyed_name(case['forced'])}/{n} | "
               if case["expected_tool"] else "— | ")
            + (f"{_called_anything(case['control'])}/{n} |"
               if case["control"] else "— |")
        )

    lines += ["", "## По случаям", ""]
    for case in report["cases"]:
        lines += [
            f"### `{case['id']}`",
            "",
            f"Клиент: «{case['client_text']}»",
            "",
            f"{case['why']}.",
            "",
        ]
        for arm in ("forced", "control"):
            if not case[arm]:
                continue
            title = "С принуждением" if arm == "forced" else "Без принуждения (контроль)"
            lines.append(f"**{title}:**")
            lines.append("")
            for i, attempt in enumerate(case[arm], 1):
                tools = ", ".join(attempt["tools"]) or "— ни одного —"
                asked = attempt.get("asked_tool")
                if asked and attempt.get("first_tool") != asked:
                    tools += f"  ← просили `{asked}`, первым позвал другой"
                guard = f" · рубеж: {attempt['guard_rail']}" if attempt["guard_rail"] else ""
                err = f" · ОШИБКА: {attempt['error']}" if attempt["error"] else ""
                lines.append(f"{i}. `{tools}`{guard}{err}")
                args = attempt["tool_args"].get(case["expected_tool"] or "")
                if args:
                    lines.append(f"   - аргументы: `{json.dumps(args, ensure_ascii=False)}`")
                text = (attempt["text"] or "").replace("\n", " ")
                if text:
                    lines.append(f"   - ответ: {text[:200]}")
            lines.append("")
    return "\n".join(lines) + "\n"


async def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=5,
                        help="повторов на случай в каждом плече (по умолчанию 5)")
    parser.add_argument("--case", help="прогнать только один случай по id")
    parser.add_argument("--provider", choices=["deepseek", "anthropic"],
                        default="deepseek")
    parser.add_argument(
        "--out", type=Path,
        help="куда положить отчёт (без расширения). Нужен, когда случаи "
             "гоняются по одному и результаты потом сшиваются: живой прогон "
             "длинный, и терять уже оплаченные ответы из-за обрыва обидно",
    )
    parser.add_argument("--merge", nargs="+", type=Path,
                        help="сшить готовые json-куски в один отчёт и выйти")
    parser.add_argument("--no-control", action="store_true",
                        help="без контрольного плеча — быстрее и вдвое дешевле, "
                             "но результат сам по себе ничего не доказывает")
    args = parser.parse_args()

    if args.merge:
        pieces = [json.loads(path.read_text(encoding="utf-8")) for path in args.merge]
        report = dict(pieces[0])
        # Порядок случаев — как в CASES, а не как в порядке файлов: отчёт
        # читают глазами, и он не должен переставляться от прогона к прогону.
        order = {case.id: i for i, case in enumerate(CASES)}
        report["cases"] = sorted(
            [c for piece in pieces for c in piece["cases"]],
            key=lambda c: order.get(c["id"], len(order)),
        )
    else:
        report = await probe(args.repeats, args.case, args.provider,
                             control=not args.no_control)

    json_path = args.out.with_suffix(".json") if args.out else REPORT_JSON
    md_path = args.out.with_suffix(".md") if args.out else REPORT_MD
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    md_path.write_text(render(report), encoding="utf-8")

    n = report["repeats"]
    print(f"\nПровайдер {report['provider']}, модель {report['model']}, "
          f"{n} повторов на случай\n")
    for case in report["cases"]:
        control = (f"контроль {_called_anything(case['control'])}/{n}"
                   if case["control"] else "контроль не запускался")
        if case["expected_tool"] is None:
            # Здесь успех — НЕ позвать. Печатать «позвал 0/5» как достижение
            # рядом с остальными строками значило бы читать таблицу задом
            # наперёд через раз.
            print(f"{case['id']:<24} принуждать нечего        "
                  f"полез в инструменты {_called_anything(case['forced'])}/{n}   "
                  f"{control}")
            continue
        print(f"{case['id']:<24} просим {case['expected_tool']:<20} "
              f"позвал {_called_anything(case['forced'])}/{n}   "
              f"именно его {_obeyed_name(case['forced'])}/{n}   {control}")
    print(f"\nОтчёт: {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
