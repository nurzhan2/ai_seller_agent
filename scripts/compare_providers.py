"""Сравнительный прогон Anthropic vs DeepSeek на одних и тех же 27 диалогах.

    python -m scripts.compare_providers

Требует ключи обоих провайдеров (ANTHROPIC_API_KEY, DEEPSEEK_API_KEY) для
честного сравнения. Если ключа одного из провайдеров нет — скрипт не
отказывается работать (как и scripts/replay.py), но результат для него будет
офлайн-заглушкой, и это явно написано в отчёте прописными буквами, а не
спрятано в сноске: заглушка никогда не называет цену и не вызывает
инструменты, поэтому у неё всегда 0 нарушений — это не значит, что провайдер
«лучше», это значит, что для него не было прогона.

Судья (claude-opus-5) в обоих прогонах — один и тот же реальный Anthropic-
клиент (см. scripts/replay.py:build_judge_client) — иначе сравнение баллов
тона было бы бессмысленным, промт №12 явно это требует.
"""

from __future__ import annotations

import asyncio
import statistics
import sys
from pathlib import Path

from app.quality.report import RunResult
from scripts.replay import replay

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "PROVIDER_COMPARISON.md"


def _median(values: list[float]) -> float:
    return round(statistics.median(values), 1) if values else 0.0


def _rate(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "н/д"
    return f"{numerator / denominator:.0%}"


def _metrics(result: RunResult) -> dict:
    turns = result.turns
    with_tools = [t for t in turns if t.tool_calls]
    clean_first_try = [t for t in with_tools if t.tool_call_errors == 0]
    dialogs = {t.dialog_id for t in turns}
    total_cost = sum(float(t.cost_rub) for t in turns)
    return {
        "total_turns": len(turns),
        "violations": len(result.failed_turns),
        "violations_by_rule": result.as_json()["violations_by_rule"],
        "hit_limit": sum(1 for t in turns if t.hit_iteration_limit),
        "tool_calls_total": len(with_tools),
        "tool_first_try_rate": _rate(len(clean_first_try), len(with_tools)),
        "avg_reply_len": round(
            statistics.mean(len(t.agent_text) for t in turns), 0
        ) if turns else 0,
        "median_latency_ms": _median([t.latency_ms for t in turns]),
        "total_cost_rub": round(total_cost, 2),
        "cost_per_dialog_rub": round(total_cost / len(dialogs), 2) if dialogs else 0,
        "judge": result.judge_summary,
    }


def _diff_section(anthropic: RunResult, deepseek: RunResult) -> str:
    if not anthropic.model_available or not deepseek.model_available:
        return (
            "**Недоступно в этом прогоне.** Один из провайдеров (или оба) работал "
            "в офлайн-режиме — заглушка никогда не называет цену и не вызывает "
            "инструменты, поэтому у неё всегда 0 нарушений. Сравнивать её "
            "«чистоту» с реальными ответами другого провайдера значило бы "
            "утверждать, что заглушка «лучше», а это неправда — для неё просто "
            "не было настоящего прогона. Дождитесь запуска с обоими ключами."
        )

    lines = []
    for a_turn, d_turn in zip(anthropic.turns, deepseek.turns):
        if a_turn.dialog_id != d_turn.dialog_id or a_turn.client_text != d_turn.client_text:
            return (
                "**Не удалось сопоставить ходы между прогонами** — порядок диалогов "
                "разошёлся. Возможно, docs/analysis/dialogs.json изменился между "
                "прогонами."
            )
        if d_turn.violations and not a_turn.violations:
            rules = ", ".join(v.rule for v in d_turn.violations)
            lines.append(
                f"- **{d_turn.dialog_id}** ({rules}) — клиент: «{d_turn.client_text[:150]}» "
                f"→ DeepSeek: «{d_turn.agent_text[:250]}»"
            )
    if not lines:
        return "Таких ходов не найдено — там, где DeepSeek нарушал правило, Anthropic тоже нарушал (или не нарушал вовсе)."
    return "\n".join(lines)


def _fmt_judge(judge: dict) -> str:
    if not judge.get("judged"):
        return "н/д (судья не запускался)"
    return (
        f"тон {judge['tone']}, польза {judge['usefulness']}, "
        f"к сделке {judge['deal_progress']}, живость {judge['naturalness']} "
        f"(из {judge['judged']} оценённых, лучше менеджера в {judge['better_than_manager']})"
    )


async def main(limit: int | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print("Прогон Anthropic...")
    anthropic_result = await replay(limit=limit, judge_enabled=True, provider_name="anthropic")
    print("Прогон DeepSeek...")
    deepseek_result = await replay(limit=limit, judge_enabled=True, provider_name="deepseek")

    a = _metrics(anthropic_result)
    d = _metrics(deepseek_result)

    def _status_line(result: RunResult, key_name: str) -> str:
        if not result.model_available:
            return f"⚠️ офлайн-заглушка ({key_name} не задан) — числа ниже НЕ отражают реальное качество"
        if not result.judge_available:
            return "агент реальный, но судья не запускался (нужен ANTHROPIC_API_KEY)"
        return "полный прогон, включая судью"

    lines = [
        "# Сравнение провайдеров: Anthropic vs DeepSeek",
        "",
        "Прогон харнесса (`scripts/compare_providers.py`) по 27 размеченным диалогам "
        "из `docs/analysis/dialogs.json`. Судья в обоих прогонах — один и тот же "
        "реальный Anthropic-клиент (claude-opus-5), не тот провайдер, что под тестом.",
        "",
        f"- Anthropic: {_status_line(anthropic_result, 'ANTHROPIC_API_KEY')}",
        f"- DeepSeek: {_status_line(deepseek_result, 'DEEPSEEK_API_KEY')}",
        "",
        "## Таблица",
        "",
        "| метрика | Anthropic | DeepSeek |",
        "|---|---|---|",
        f"| ходов всего | {a['total_turns']} | {d['total_turns']} |",
        f"| нарушений жёстких проверок | {a['violations']} | {d['violations']} |",
        f"| доля ходов, упёршихся в лимит витков (5) | {_rate(a['hit_limit'], a['total_turns'])} | {_rate(d['hit_limit'], d['total_turns'])} |",
        f"| инструмент вызван корректно с первой попытки | {a['tool_first_try_rate']} ({a['tool_calls_total']} вызовов) | {d['tool_first_try_rate']} ({d['tool_calls_total']} вызовов) |",
        f"| средняя длина ответа, символов | {a['avg_reply_len']:.0f} | {d['avg_reply_len']:.0f} |",
        f"| медианная задержка хода, мс | {a['median_latency_ms']} | {d['median_latency_ms']} |",
        f"| стоимость прогона всего, ₽ | {a['total_cost_rub']} | {d['total_cost_rub']} |",
        f"| стоимость на диалог, ₽ | {a['cost_per_dialog_rub']} | {d['cost_per_dialog_rub']} |",
        f"| баллы судьи | {_fmt_judge(a['judge'])} | {_fmt_judge(d['judge'])} |",
        "",
    ]

    if not anthropic_result.model_available or not deepseek_result.model_available:
        lines += [
            "> ⚠️ **Задержка и стоимость несопоставимы, если один из провайдеров шёл "
            "офлайн.** Заглушка отвечает мгновенно и бесплатно — это не значит, что "
            "реальный провайдер будет таким же. Сравнивайте эти строки только тогда, "
            "когда обе стороны отмечены выше как «полный прогон».",
            "",
        ]

    lines += [
        "## Нарушения по правилам",
        "",
        "| правило | Anthropic | DeepSeek |",
        "|---|---|---|",
    ]
    all_rules = sorted(set(a["violations_by_rule"]) | set(d["violations_by_rule"]))
    if not all_rules:
        lines.append("| — | 0 | 0 |")
    for rule in all_rules:
        lines.append(f"| {rule} | {a['violations_by_rule'].get(rule, 0)} | {d['violations_by_rule'].get(rule, 0)} |")
    lines.append("")

    lines += [
        "## Ходы, где DeepSeek нарушил правило, а Anthropic — нет",
        "",
        _diff_section(anthropic_result, deepseek_result),
        "",
        "## Известные архитектурные различия провайдеров",
        "",
        "Не измеряется таблицей выше, но напрямую влияет на надёжность —"
        " сверено живыми вызовами `api.deepseek.com/anthropic` 2026-08-20,"
        " не только по документации:",
        "",
        "- **Нет strict mode.** У DeepSeek `strict: true` существует только на"
        " их OpenAI-совместимом `/beta` эндпоинте, с другой формой описания"
        " инструментов (`function.parameters`, а не `input_schema`). На"
        " Anthropic-совместимом пути, которым идёт этот проект, строки"
        " `strict` в таблице совместимости инструментов нет вообще — заявлено"
        " «не будем переписывать под другой SDK», после чего явно решено не"
        " держаться за строгую схему аргументов ценой большого слоя"
        " трансляции. Вместо неё — двойная защита: pydantic/dataclass-валидация"
        " аргументов внутри самих инструментов (ошибка возвращается"
        " модели как `tool_result`, а не роняет ход — см."
        " `test_malformed_tool_args_return_recoverable_error_not_a_crash`) и"
        " последний рубеж в `app/agent/loop.py` (цена в тексте без вызова"
        " инструмента — эскалация, независимо от провайдера).",
        "- **DeepSeek v4 по умолчанию думает перед ответом** (`thinking`-блок)."
        " При маленьком `max_tokens` (наш классификатор просит 8) модель"
        " может исчерпать лимит целиком на размышлении и вернуть пустой ответ"
        " — воспроизведено: `max_tokens=8` без `thinking: {\"type\":"
        " \"disabled\"}` → `stop_reason == \"max_tokens\"`, ни одного"
        " текстового блока. Отключено по умолчанию"
        " (`DEEPSEEK_ENABLE_THINKING=false`) — без этого либо ломается"
        " классификатор, либо стоимость/задержка растут непредсказуемо не"
        " из-за качества ответа, а из-за скрытого размышления.",
        "- **`cache_control` через Anthropic-совместимый путь DeepSeek"
        " молча игнорируется** — не падает, но и не даёт подтверждённой"
        " скидки на повторяющийся системный промт с каталогом. Оценка"
        " стоимости DeepSeek в таблице выше поэтому взята по WORST CASE"
        " (peak-тариф, `cache miss`) — реальная стоимость может оказаться"
        " ниже, но гарантированно не выше.",
        "",
    ]

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nОтчёт записан: {OUT}")
    return 0


if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else None
    raise SystemExit(asyncio.run(main(limit)))
