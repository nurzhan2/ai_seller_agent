"""Прогон агента по 27 размеченным диалогам.

    python -m scripts.replay                          # Anthropic, прогон и отчёт
    python -m scripts.replay --baseline               # записать результат как эталон
    python -m scripts.replay --limit 5                # быстрый прогон нескольких диалогов
    python -m scripts.replay --provider deepseek      # тот же прогон на DeepSeek

Без ключа выбранного провайдера (ANTHROPIC_API_KEY / DEEPSEEK_API_KEY) скрипт
не падает, а переходит в офлайн-режим: ответы агента берутся из
детерминированной заглушки, судья не вызывается. Жёсткие проверки при этом
работают по-настоящему — так можно убедиться, что харнесс ловит нарушения,
ещё до того, как появится ключ.

Судья (Judge, claude-opus-5) всегда требует ANTHROPIC_API_KEY, даже когда
--provider deepseek: промт №12, Часть 4 требует одну и ту же модель-судью для
обоих прогонов, иначе сравнение бессмысленно. Агент под тестом и судья —
всегда разные клиенты, даже если оба указывают на Anthropic.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

from app.agent.loop import AgentLoop
from app.agent.providers.deepseek_provider import BASE_URL as DEEPSEEK_BASE_URL
from app.agent.providers.deepseek_provider import DeepSeekProvider
from app.agent.providers.factory import default_models_for
from app.agent.tools import ToolExecutor
from app.kb.loader import load_catalog
from app.quality.asserts import TurnUnderTest, check_turn
from app.quality.judge import Judge, JudgeSummary
from app.quality.report import RunResult, TurnRecord, compare_to_baseline, write_report

ROOT = Path(__file__).resolve().parent.parent
DIALOGS = ROOT / "docs" / "analysis" / "dialogs.json"
REPORT_HTML = ROOT / "docs" / "quality" / "replay_report.html"
REPORT_JSON = ROOT / "docs" / "quality" / "replay_result.json"
BASELINE = ROOT / "tests" / "baseline.json"


# --------------------------------------------------------------------------
# Офлайн-заглушка модели
# --------------------------------------------------------------------------

class _Block:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _Usage:
    input_tokens = 0
    output_tokens = 0
    cache_read_input_tokens = 0


class _Response:
    def __init__(self, text: str):
        self.content = [_Block(text)]
        self.usage = _Usage()


class OfflineMessages:
    """Отвечает заготовленной фразой.

    Намеренно даёт корректный по правилам ответ: смысл офлайн-прогона не в
    оценке качества текста, а в проверке того, что конвейер и жёсткие
    проверки работают от начала до конца.
    """

    def __init__(self):
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        model = kwargs.get("model", "")
        # "haiku" — классификатор Anthropic, "flash" — классификатор
        # DeepSeek (deepseek-v4-flash). Оба провайдера должны получать
        # заглушку-классификацию, а не заглушку основного диалога.
        if "haiku" in model or "flash" in model:
            return _Response("question")
        return _Response("Здравствуйте! Подскажите, на какое число планируете отдых?")


class OfflineClient:
    def __init__(self):
        self.messages = OfflineMessages()


def build_agent_provider(provider_name: str) -> tuple[Any, bool]:
    """Клиент/провайдер для АГЕНТА под тестом — anthropic или deepseek."""
    if provider_name == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return OfflineClient(), False
        try:
            from anthropic import AsyncAnthropic
        except ImportError:
            print("anthropic не установлен — офлайн-режим", file=sys.stderr)
            return OfflineClient(), False
        return AsyncAnthropic(api_key=api_key), True

    if provider_name == "deepseek":
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            return OfflineClient(), False
        try:
            import anthropic  # noqa: F401  (используется через DeepSeekProvider)
        except ImportError:
            print("anthropic не установлен — офлайн-режим", file=sys.stderr)
            return OfflineClient(), False
        return DeepSeekProvider(api_key=api_key, base_url=DEEPSEEK_BASE_URL), True

    raise ValueError(f"неизвестный провайдер {provider_name!r}")


def build_judge_client() -> tuple[Any, bool]:
    """Клиент СУДЬИ — всегда Anthropic/claude-opus-5, независимо от того,
    какой провайдер сейчас под тестом (промт №12, Часть 4: одна и та же
    модель-судья для обоих прогонов, иначе сравнение бессмысленно)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return OfflineClient(), False
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        return OfflineClient(), False
    return AsyncAnthropic(api_key=api_key), True


# --------------------------------------------------------------------------
# Прогон
# --------------------------------------------------------------------------

def load_dialogs(limit: Optional[int] = None) -> list[dict]:
    data = json.loads(DIALOGS.read_text(encoding="utf-8"))
    return data[:limit] if limit else data


def _next_manager_reply(turns: list[dict], index: int) -> str:
    for turn in turns[index + 1:]:
        if turn.get("role") == "manager":
            return turn.get("text", "")
        if turn.get("role") == "client":
            break
    return ""


async def replay(
    limit: Optional[int] = None,
    judge_enabled: bool = True,
    provider_name: str = "anthropic",
) -> RunResult:
    kb = load_catalog()
    agent_client, online = build_agent_provider(provider_name)
    dialog_model, classifier_model = default_models_for(provider_name)
    agent = AgentLoop(agent_client, kb, dialog_model=dialog_model, classifier_model=classifier_model)

    judge_client, judge_online = build_judge_client()
    judge = Judge(judge_client) if (judge_online and judge_enabled) else None
    summary = JudgeSummary()

    known_zone_ids = [z.id for z in kb.catalog.zones]
    result = RunResult(model_available=online, judge_available=judge_online, provider=provider_name)

    for dialog in load_dialogs(limit):
        dialog_id = dialog["id"]
        history: list[dict] = []
        # Состояние уступок живёт на весь диалог — как в проде.
        executor = ToolExecutor(kb, dialog_id)
        agent.executor_factory = lambda did, state, _ex=executor: _ex

        turns = dialog.get("turns", [])
        for index, turn in enumerate(turns):
            if turn.get("role") != "client":
                continue
            client_text = turn.get("text", "")
            if not client_text.strip():
                continue

            started_at = time.monotonic()
            outcome = await agent.run_turn(dialog_id, history, client_text)
            latency_ms = (time.monotonic() - started_at) * 1000

            history.append({"role": "user", "content": client_text})
            if outcome.text:
                history.append({"role": "assistant", "content": outcome.text})

            quoted_totals = [
                str(executor.last_quote.total)
                for _ in [0]
                if executor.last_quote is not None and executor.last_quote.total is not None
            ]

            violations = check_turn(
                TurnUnderTest(
                    text=outcome.text,
                    tool_calls=outcome.tool_calls,
                    quote_statuses=outcome.quote_statuses,
                    quoted_totals=quoted_totals,
                    concession_granted=bool(outcome.granted_offer_templates),
                    known_zone_ids=known_zone_ids,
                    applied_promo=(
                        executor.last_quote.applied_promo if executor.last_quote else None
                    ),
                )
            )

            manager_text = _next_manager_reply(turns, index)
            record = TurnRecord(
                dialog_id=dialog_id,
                zone_topic=dialog.get("zone_topic", ""),
                client_text=client_text,
                manager_text=manager_text,
                agent_text=outcome.text,
                tool_calls=list(outcome.tool_calls),
                quote_statuses=list(outcome.quote_statuses),
                violations=violations,
                latency_ms=latency_ms,
                hit_iteration_limit=outcome.hit_iteration_limit,
                tool_call_errors=outcome.tool_call_errors,
                cost_rub=str(outcome.llm_meta.get("cost_rub", "0")),
            )

            if judge is not None:
                score = await judge.score(client_text, manager_text, outcome.text)
                summary.scores.append(score)
                record.scores = {
                    "average": score.average,
                    "why": score.why,
                    "better_than_manager": score.better_than_manager,
                }

            result.turns.append(record)

    if judge is not None:
        result.judge_summary = summary.as_dict()
    return result


async def main() -> int:
    # Отчёт русскоязычный и с эмодзи; консоль Windows по умолчанию cp1251.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true", help="записать результат как эталон")
    parser.add_argument("--limit", type=int, help="прогнать только N диалогов")
    parser.add_argument("--no-judge", action="store_true", help="без оценок судьи")
    parser.add_argument(
        "--provider", choices=["anthropic", "deepseek"], default="anthropic",
        help="какой провайдер обслуживает агента под тестом (промт №12)",
    )
    args = parser.parse_args()

    if args.baseline and args.provider != "anthropic":
        print(
            "❌ --baseline пишет tests/baseline.json — эталон регрессии для продакшен-"
            "провайдера (anthropic). Прогон на deepseek для baseline не годится: "
            "используйте scripts/compare_providers.py для сравнения.",
            file=sys.stderr,
        )
        return 2

    result = await replay(limit=args.limit, judge_enabled=not args.no_judge, provider_name=args.provider)
    write_report(result, REPORT_HTML, REPORT_JSON)
    data = result.as_json()

    print(f"Провайдер: {args.provider}")
    print(f"Ходов: {data['total_turns']}, с нарушениями: {data['failed_turns']}")
    print(f"Доля чистых: {data['pass_rate']:.0%}")
    if data["violations_by_rule"]:
        print("Нарушения:")
        for rule, count in sorted(data["violations_by_rule"].items(), key=lambda kv: -kv[1]):
            print(f"  {rule}: {count}")
    key_name = "DEEPSEEK_API_KEY" if args.provider == "deepseek" else "ANTHROPIC_API_KEY"
    if not result.model_available:
        print(f"\n⚠️  Офлайн-режим: {key_name} не задан, ответы агента — заглушка.")
    elif not result.judge_available:
        print("\n⚠️  Агент отвечал по-настоящему, но судья — нет (нужен ANTHROPIC_API_KEY).")
    print(f"\nОтчёт: {REPORT_HTML}")

    if args.baseline:
        BASELINE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Эталон записан: {BASELINE}")
        return 0

    if BASELINE.exists():
        problems = compare_to_baseline(data, json.loads(BASELINE.read_text(encoding="utf-8")))
        if problems:
            print("\n❌ РЕГРЕССИЯ относительно эталона:")
            for problem in problems:
                print(f"  • {problem}")
            return 1
        print("\n✅ Регрессий нет.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
