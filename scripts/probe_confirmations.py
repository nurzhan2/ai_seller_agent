"""Живой замер: не уводит ли подтверждение клиента к оператору.

    python -m scripts.probe_confirmations              # 5 повторов на фразу
    python -m scripts.probe_confirmations --repeats 10

ЗАЧЕМ. Замер 2026-09-18 (scripts/probe_questions.py, случай
everything_is_known): клиент назвал зону, дату, время и гостей, написал «ну
что, подойдёт?» — и в трёх ответах из пяти получил «передала вопрос
менеджеру». Разбор показал, что дело в классификаторе: он видел одну фразу
без переписки и ставил метку human на «ок» (4 из 5), «давайте» (4 из 5),
«годится», «ну что, подойдёт?». Метка human уводит к оператору сразу, мимо
основной модели. Это ровно момент, когда клиент готов бронировать.

Починка — два слоя (app/agent/loop.py): классификатор получает последнюю
реплику администратора и метку confirm, а вето кодом снимает human, если в
тексте нет явной просьбы о человеке или жалобы.

ЧТО СЧИТАЕТСЯ. Две разные величины, и их нельзя смешивать:
  * сырая метка классификатора — сколько раз он всё ещё говорит human.
    Это поведение модели, оно ненулевым быть может: для этого и вето;
  * ушёл ли ХОД к человеку по метке — ответ HANDED_TO_MANAGER без вызова
    основной модели. Обязано быть нулём.

Эскалация ИНСТРУМЕНТОМ считается отдельно и не провалом: при
`payment.handoff_on_payment_step=true` согласие на бронь законно передаётся
оператору вместе с карточкой брони — это решение заказчика, а не ошибка.
Провал — только если причина эскалации «клиент просит человека».

Календарь — та же заглушка «свободно с 13:00», что у probe_questions.

Отчёт: docs/quality/confirmations_probe.md (+ .json рядом).
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import sys
from pathlib import Path
from typing import Any

from app.agent.loop import HANDED_TO_MANAGER, AgentLoop
from app.agent.providers.base import LLMProvider
from app.agent.providers.deepseek_provider import BASE_URL as DEEPSEEK_BASE_URL
from app.agent.providers.deepseek_provider import DeepSeekProvider
from app.agent.providers.factory import default_models_for
from app.agent.tools import ToolExecutor
from app.kb.loader import load_catalog
from scripts.probe_questions import _FreeCalendar

ROOT = Path(__file__).resolve().parent.parent
REPORT_MD = ROOT / "docs" / "quality" / "confirmations_probe.md"
REPORT_JSON = ROOT / "docs" / "quality" / "confirmations_probe.json"

# Диалог из замера, где всё и началось: клиент назвал всё, администратор
# ответил расчётом и спросил, бронируем ли.
HISTORY = (
    {"role": "user", "content": "баня Русский стиль, 20 сентября, с 13 до 17, нас шестеро"},
    {"role": "assistant",
     "content": "«Русский стиль» 20 сентября с 13:00 до 17:00 свободна. Выходной "
                "тариф 3500 ₽/час, за 4 часа выйдет 14 000 ₽. Бронируем?"},
)

PHRASES = ("ну что, подойдёт?", "ок", "да, берём", "годится", "давайте")

# Причина эскалации, которую ставит путь по метке human (см. run_turn).
HUMAN_PATH_REASON = "клиент просит человека / жалоба"


class _LabelRecorder(LLMProvider):
    """Обёртка провайдера: запоминает сырую метку классификатора.

    Наследник LLMProvider ОБЯЗАТЕЛЬНО: AgentLoop принимает всё остальное за
    «сырой» anthropic-клиент и зовёт у него .messages — первый прогон упал
    так на всех 25 попытках.
    """

    def __init__(self, inner: Any, classifier_model: str):
        self.inner = inner
        self.name = inner.name
        self.classifier_model = classifier_model
        self.labels: list[str] = []

    @property
    def supports_prompt_caching(self) -> bool:
        return self.inner.supports_prompt_caching

    def estimate_cost(self, *args, **kwargs):
        return self.inner.estimate_cost(*args, **kwargs)

    async def complete(self, **kwargs):
        response = await self.inner.complete(**kwargs)
        if kwargs.get("model") == self.classifier_model:
            text = "".join(getattr(b, "text", "") for b in response.content).strip().lower()
            self.labels.append(text.split()[0] if text else "")
        return response


async def probe(repeats: int) -> dict:
    from anthropic import AsyncAnthropic

    from app.config import get_settings

    settings = get_settings()
    api_key = (settings.deepseek_api_key.get_secret_value()
               or os.environ.get("DEEPSEEK_API_KEY", ""))
    if not api_key:
        raise SystemExit("нет DEEPSEEK_API_KEY — замер живой, заглушка здесь бессмысленна")

    kb = load_catalog()
    dialog_model, classifier_model = default_models_for("deepseek")
    # Свой клиент с запасом повторов: замер не должен падать на сетевых
    # обрывах машины, с которой его запускают. Прод-настройки не трогаются.
    raw = AsyncAnthropic(api_key=api_key, base_url=DEEPSEEK_BASE_URL,
                         max_retries=8, timeout=90)
    provider = _LabelRecorder(
        DeepSeekProvider(client=raw, enable_thinking=settings.deepseek_enable_thinking),
        classifier_model,
    )
    agent = AgentLoop(provider, kb, dialog_model=dialog_model,
                      classifier_model=classifier_model)

    report: dict = {"model": dialog_model, "classifier": classifier_model,
                    "repeats": repeats, "phrases": []}
    for phrase in PHRASES:
        entry = {"phrase": phrase, "attempts": []}
        for _ in range(repeats):
            executor = ToolExecutor(kb, "probe-confirm", booking_provider=_FreeCalendar())
            agent.executor_factory = lambda did, state, _ex=executor: _ex
            provider.labels.clear()
            try:
                result = await agent.run_turn("probe-confirm", list(HISTORY), phrase)
            except Exception as exc:  # noqa: BLE001
                entry["attempts"].append({"error": f"{type(exc).__name__}: {exc}"})
                continue
            attempt = {
                "raw_label": provider.labels[0] if provider.labels else "",
                "final_label": result.classification,
                "human_path": result.escalation_reason == HUMAN_PATH_REASON,
                "escalated": result.escalated,
                "escalation_reason": result.escalation_reason,
                "tools": list(result.tool_calls),
                "text": result.text,
            }
            entry["attempts"].append(attempt)
            print(f"  [{phrase}] метка {attempt['raw_label']} -> {attempt['final_label']}"
                  f"{'  К ЧЕЛОВЕКУ ПО МЕТКЕ' if attempt['human_path'] else ''}",
                  file=sys.stderr)
        report["phrases"].append(entry)
    return report


def _ok(attempts: list[dict]) -> list[dict]:
    return [a for a in attempts if not a.get("error")]


def render(report: dict) -> str:
    n = report["repeats"]
    everything = [a for p in report["phrases"] for a in _ok(p["attempts"])]
    raw_human = sum(1 for a in everything if a["raw_label"] == "human")
    human_path = sum(1 for a in everything if a["human_path"])
    lines = [
        "# Живой замер: подтверждение клиента и оператор",
        "",
        f"Классификатор `{report['classifier']}`, диалог `{report['model']}`, "
        f"по **{n}** повторов на фразу. Устройство и смысл метрик — в "
        "докстринге `scripts/probe_confirmations.py`.",
        "",
        "| величина | значение |",
        "|---|---|",
        f"| классификатор сказал human (сырая метка) | {raw_human}/{len(everything)} |",
        f"| ход ушёл к человеку по метке | **{human_path}/{len(everything)}** |",
        "",
        "До починки (2026-09-18, та же модель, без контекста): «ок» human 4/5, "
        "«давайте» 4/5, «годится» 2/5, «ну что, подойдёт?» 2/5, «да, берём» 0/5.",
        "",
    ]
    for p in report["phrases"]:
        labels = collections.Counter(a["raw_label"] for a in _ok(p["attempts"]))
        lines += [f"## «{p['phrase']}»", "", f"сырые метки: {dict(labels)}", ""]
        for i, a in enumerate(p["attempts"], 1):
            if a.get("error"):
                lines.append(f"{i}. ⚠️ {a['error'][:200]}")
                continue
            mark = " 🚫 К ЧЕЛОВЕКУ ПО МЕТКЕ" if a["human_path"] else ""
            esc = f" (эскалация: {a['escalation_reason']})" if a["escalated"] and not a["human_path"] else ""
            lines.append(f"{i}.{mark}{esc} {a['text'][:300].replace(chr(10), ' / ')}")
        lines.append("")
    broken = sum(1 for p in report["phrases"] for a in p["attempts"] if a.get("error"))
    if broken:
        lines.append(f"> ⚠️ Упавших попыток: {broken}.")
    return "\n".join(lines) + "\n"


async def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()

    report = await probe(args.repeats)
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    REPORT_MD.write_text(render(report), encoding="utf-8")

    for p in report["phrases"]:
        done = _ok(p["attempts"])
        labels = collections.Counter(a["raw_label"] for a in done)
        print(f"{p['phrase']!r:24} сырые метки {dict(labels)}   "
              f"к человеку по метке {sum(a['human_path'] for a in done)}/{len(done)}   "
              f"эскалаций инструментом {sum(a['escalated'] and not a['human_path'] for a in done)}")
    print(f"\nОтчёт: {REPORT_MD}")

    # Упавшая попытка — не «не ушёл к человеку». Первый прогон упал целиком
    # и напечатал «0/0» строками, похожими на успех; больше так нельзя.
    total = sum(len(p["attempts"]) for p in report["phrases"])
    broken = sum(1 for p in report["phrases"] for a in p["attempts"] if a.get("error"))
    if broken:
        print(f"⚠️ упавших попыток: {broken}/{total}", file=sys.stderr)
    return 1 if broken * 2 >= total else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
