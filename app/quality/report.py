"""HTML-отчёт прогона: диффы «менеджер / агент», баллы, FAIL сверху красным."""

from __future__ import annotations

import html
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.quality.asserts import Violation


@dataclass
class TurnRecord:
    dialog_id: str
    zone_topic: str
    client_text: str
    manager_text: str
    agent_text: str
    tool_calls: list[str] = field(default_factory=list)
    quote_statuses: list[str] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    scores: Optional[dict] = None
    # Метрики для сравнения провайдеров (промт №12, Часть 4).
    latency_ms: float = 0.0
    hit_iteration_limit: bool = False
    tool_call_errors: int = 0
    cost_rub: str = "0"


@dataclass
class RunResult:
    turns: list[TurnRecord] = field(default_factory=list)
    judge_summary: dict = field(default_factory=dict)
    model_available: bool = True
    # Судья (claude-opus-5) требует ANTHROPIC_API_KEY независимо от того,
    # какой провайдер обслуживал агента — промт №12 требует одну и ту же
    # модель-судью в обоих сравниваемых прогонах. Раньше это поле было тем
    # же, что model_available; после появления DeepSeek это два разных
    # факта: агент мог реально отвечать, а судья — нет.
    judge_available: bool = True
    provider: str = "anthropic"

    @property
    def failed_turns(self) -> list[TurnRecord]:
        return [t for t in self.turns if t.violations]

    def as_json(self) -> dict:
        return {
            "total_turns": len(self.turns),
            "failed_turns": len(self.failed_turns),
            "pass_rate": round(
                1 - len(self.failed_turns) / len(self.turns), 3
            ) if self.turns else 0.0,
            "violations_by_rule": self._violations_by_rule(),
            "judge": self.judge_summary,
            "model_available": self.model_available,
            "judge_available": self.judge_available,
            "provider": self.provider,
        }

    def _violations_by_rule(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for turn in self.turns:
            for violation in turn.violations:
                counts[violation.rule] = counts.get(violation.rule, 0) + 1
        return counts


_CSS = """
body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f6f7f9;color:#1a1a1a}
.wrap{max-width:1100px;margin:0 auto;padding:24px}
h1{font-size:24px;margin:0 0 4px}
.sub{color:#666;margin-bottom:24px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:24px}
.card{background:#fff;border:1px solid #e3e5e8;border-radius:10px;padding:14px 18px;min-width:130px}
.card .n{font-size:26px;font-weight:600}
.card .l{color:#666;font-size:13px}
.fail .n{color:#c0392b}
.ok .n{color:#1e8449}
.banner{background:#fff4f4;border:1px solid #f0b4b4;border-radius:10px;padding:14px 18px;margin-bottom:24px}
.banner.warn{background:#fff9e6;border-color:#f0d9a0}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e3e5e8;border-radius:10px;overflow:hidden}
th,td{padding:10px 12px;text-align:left;vertical-align:top;border-bottom:1px solid #eef0f2;font-size:14px}
th{background:#fafbfc;font-weight:600;font-size:13px;color:#555}
tr.bad{background:#fff6f6}
.rule{display:inline-block;background:#c0392b;color:#fff;border-radius:4px;padding:1px 6px;font-size:11px;margin:1px 2px 1px 0}
.mgr{color:#666}
.agent{color:#1a1a1a}
.score{font-variant-numeric:tabular-nums;white-space:nowrap}
"""


def render_html(result: RunResult, title: str = "Прогон на реальных диалогах") -> str:
    data = result.as_json()
    e = html.escape

    parts = [
        "<!-- отчёт прогона -->",
        f"<style>{_CSS}</style>",
        '<div class="wrap">',
        f"<h1>{e(title)}</h1>",
        f'<div class="sub">27 размеченных диалогов · docs/analysis/dialogs.json</div>',
    ]

    key_name = "DEEPSEEK_API_KEY" if result.provider == "deepseek" else "ANTHROPIC_API_KEY"
    if not result.model_available:
        parts.append(
            f'<div class="banner warn"><b>Прогон выполнен без обращения к модели ({e(result.provider)}).</b> '
            f"{key_name} не задан, поэтому ответы агента взяты из детерминированной "
            "заглушки, а судья не запускался. Проверки корректности при этом реальные — "
            "они и подтвердили, что харнесс ловит нарушения. Цифры тона и сравнения с "
            f"менеджерами появятся после прогона с ключом.</div>"
        )
    elif not result.judge_available:
        parts.append(
            f'<div class="banner warn"><b>Агент ({e(result.provider)}) отвечал по-настоящему, '
            "но судья — нет.</b> Судья всегда работает на claude-opus-5 и требует "
            "ANTHROPIC_API_KEY отдельно от ключа агента — без него нет баллов тона и "
            "сравнения с менеджером, но жёсткие проверки ниже настоящие.</div>"
        )

    fail_class = "fail" if data["failed_turns"] else "ok"
    parts.append('<div class="cards">')
    parts.append(f'<div class="card"><div class="n">{data["total_turns"]}</div><div class="l">ходов</div></div>')
    parts.append(
        f'<div class="card {fail_class}"><div class="n">{data["failed_turns"]}</div>'
        '<div class="l">с нарушениями</div></div>'
    )
    parts.append(
        f'<div class="card"><div class="n">{data["pass_rate"]:.0%}</div><div class="l">чистых ходов</div></div>'
    )
    judge = data.get("judge") or {}
    if judge.get("judged"):
        parts.append(
            f'<div class="card"><div class="n">{judge.get("overall", 0)}</div>'
            '<div class="l">средний балл</div></div>'
        )
        parts.append(
            f'<div class="card"><div class="n">{judge.get("better_than_manager", 0)}</div>'
            '<div class="l">лучше менеджера</div></div>'
        )
    parts.append("</div>")

    if data["violations_by_rule"]:
        rows = "".join(
            f"<tr><td>{e(rule)}</td><td>{count}</td></tr>"
            for rule, count in sorted(
                data["violations_by_rule"].items(), key=lambda kv: -kv[1]
            )
        )
        parts.append(
            '<div class="banner"><b>Нарушения по правилам</b>'
            f"<table><tr><th>правило</th><th>раз</th></tr>{rows}</table></div>"
        )

    # FAIL сверху.
    ordered = result.failed_turns + [t for t in result.turns if not t.violations]

    parts.append(
        "<table><tr><th>диалог</th><th>клиент</th><th>менеджер (как было)</th>"
        "<th>агент</th><th>проверки</th><th>баллы</th></tr>"
    )
    for turn in ordered:
        css = ' class="bad"' if turn.violations else ""
        rules = "".join(f'<span class="rule">{e(v.rule)}</span>' for v in turn.violations)
        detail = "<br>".join(e(v.detail) for v in turn.violations)
        score = ""
        if turn.scores:
            score = (
                f'<span class="score">{turn.scores.get("average", "")}</span><br>'
                f'<small>{e(str(turn.scores.get("why", ""))[:120])}</small>'
            )
        parts.append(
            f"<tr{css}>"
            f"<td>{e(turn.dialog_id)}<br><small>{e(turn.zone_topic)}</small></td>"
            f'<td>{e(turn.client_text[:300])}</td>'
            f'<td class="mgr">{e(turn.manager_text[:300])}</td>'
            f'<td class="agent">{e(turn.agent_text[:300])}</td>'
            f"<td>{rules}<br><small>{detail}</small></td>"
            f"<td>{score}</td>"
            "</tr>"
        )
    parts.append("</table></div>")
    return "\n".join(parts)


def write_report(result: RunResult, html_path: Path, json_path: Path) -> None:
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render_html(result), encoding="utf-8")
    json_path.write_text(
        json.dumps(result.as_json(), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def compare_to_baseline(current: dict, baseline: dict) -> list[str]:
    """Список регрессий относительно baseline.json."""
    problems: list[str] = []

    if current["pass_rate"] < baseline.get("pass_rate", 0):
        problems.append(
            f"доля чистых ходов упала: {baseline['pass_rate']:.0%} → {current['pass_rate']:.0%}"
        )

    base_rules = baseline.get("violations_by_rule", {})
    for rule, count in current.get("violations_by_rule", {}).items():
        if count > base_rules.get(rule, 0):
            problems.append(f"нарушений «{rule}»: {base_rules.get(rule, 0)} → {count}")

    base_judge = baseline.get("judge") or {}
    cur_judge = current.get("judge") or {}
    if base_judge.get("overall") and cur_judge.get("overall"):
        if cur_judge["overall"] < base_judge["overall"] - 0.2:
            problems.append(
                f"средний балл: {base_judge['overall']} → {cur_judge['overall']}"
            )
    return problems
