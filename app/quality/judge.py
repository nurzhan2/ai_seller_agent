"""Судья на claude-opus-5: оценка ответа агента против ответа менеджера.

Оценки судьи — вспомогательный сигнал, а не приговор. Жёсткие проверки
(asserts.py) первичны: ответ, нарушивший правило, остаётся FAIL, даже если
судья поставил ему пятёрки.

Сравнение с реальным менеджером здесь не ради красивой цифры: заказчику нужно
показать, где агент объективно лучше — отвечает на пропущенные вопросы,
присылает фото, делает фоллоу-ап.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("parmangal.judge")

JUDGE_MODEL = "claude-opus-5"

RUBRIC = """\
Ты оцениваешь работу ИИ-администратора загородного комплекса «ПарМангал»,
который отвечает клиентам на Авито вместо живых менеджеров.

Тебе дают реплику клиента, ответ живого менеджера (как было на самом деле) и
ответ ИИ-агента.

Оцени ответ АГЕНТА по четырём шкалам от 1 до 5:
  tone          — тепло, живость, уместность обращения
  usefulness    — отвечает ли по существу и на ВСЕ заданные вопросы
  deal_progress — продвигает ли к сделке (уточняет, предлагает, зовёт)
  naturalness   — похоже на живого человека, а не на автоответчик

Затем сравни с ответом менеджера:
  better_than_manager: true/false
  why: одно предложение — чем именно лучше или хуже

Отвечай ТОЛЬКО валидным JSON без markdown:
{"tone":N,"usefulness":N,"deal_progress":N,"naturalness":N,
 "better_than_manager":true/false,"why":"..."}"""


@dataclass
class JudgeScore:
    tone: int = 0
    usefulness: int = 0
    deal_progress: int = 0
    naturalness: int = 0
    better_than_manager: bool = False
    why: str = ""
    failed: bool = False

    @property
    def average(self) -> float:
        return round(
            (self.tone + self.usefulness + self.deal_progress + self.naturalness) / 4, 2
        )


@dataclass
class JudgeSummary:
    scores: list[JudgeScore] = field(default_factory=list)

    def _mean(self, attr: str) -> float:
        usable = [s for s in self.scores if not s.failed]
        if not usable:
            return 0.0
        return round(sum(getattr(s, attr) for s in usable) / len(usable), 2)

    def as_dict(self) -> dict:
        usable = [s for s in self.scores if not s.failed]
        return {
            "tone": self._mean("tone"),
            "usefulness": self._mean("usefulness"),
            "deal_progress": self._mean("deal_progress"),
            "naturalness": self._mean("naturalness"),
            "overall": round(
                sum(s.average for s in usable) / len(usable), 2
            ) if usable else 0.0,
            "better_than_manager": sum(1 for s in usable if s.better_than_manager),
            "judged": len(usable),
        }


def _extract_json(text: str) -> Optional[dict]:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group())
    except json.JSONDecodeError:
        return None


class Judge:
    def __init__(self, client: Any, model: str = JUDGE_MODEL):
        self.client = client
        self.model = model

    async def score(self, client_text: str, manager_text: str, agent_text: str) -> JudgeScore:
        if not agent_text.strip():
            return JudgeScore(failed=True, why="агент не ответил")

        payload = (
            f"Клиент: {client_text}\n\n"
            f"Ответ живого менеджера: {manager_text or '(менеджер не ответил)'}\n\n"
            f"Ответ ИИ-агента: {agent_text}"
        )
        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=300,
                system=RUBRIC,
                messages=[{"role": "user", "content": payload}],
            )
            raw = "".join(
                b.text for b in response.content if getattr(b, "type", None) == "text"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("judge call failed: %s", exc)
            return JudgeScore(failed=True, why=f"судья недоступен: {type(exc).__name__}")

        data = _extract_json(raw)
        if data is None:
            return JudgeScore(failed=True, why="судья вернул не JSON")

        def _clamp(value: Any) -> int:
            try:
                return max(1, min(5, int(value)))
            except (TypeError, ValueError):
                return 1

        return JudgeScore(
            tone=_clamp(data.get("tone")),
            usefulness=_clamp(data.get("usefulness")),
            deal_progress=_clamp(data.get("deal_progress")),
            naturalness=_clamp(data.get("naturalness")),
            better_than_manager=bool(data.get("better_than_manager")),
            why=str(data.get("why", ""))[:400],
        )
