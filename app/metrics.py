"""Метрики Prometheus и дневной лимит расхода.

Лимит здесь, а не в админке, потому что это предохранитель, а не отчёт: при
превышении агент обязан уйти на паузу сам, без участия человека.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

logger = logging.getLogger("parmangal.metrics")

messages_total = Counter(
    "parmangal_messages_total", "Обработано сообщений", ["direction", "status"]
)
quotes_total = Counter("parmangal_quotes_total", "Котировки по статусам", ["status"])
escalations_total = Counter("parmangal_escalations_total", "Эскалации", ["reason"])
concessions_total = Counter(
    "parmangal_concessions_total", "Уступки", ["tier", "allowed"]
)
revenue_delta_rub = Counter(
    "parmangal_revenue_delta_rub", "Сумма недополученной выручки по уступкам"
)
llm_cost_rub = Counter("parmangal_llm_cost_rub", "Расход на модели, рубли", ["model"])
turn_seconds = Histogram("parmangal_turn_seconds", "Время обработки хода")
agent_paused = Gauge("parmangal_agent_paused", "1 если агент на паузе")
dry_run_gauge = Gauge("parmangal_dry_run", "1 если включён режим модерации")


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST


class DailyCostGuard:
    """Останавливает агента при превышении дневного лимита расхода.

    Считает по одному источнику с /admin/costs, чтобы «в отчёте одно, в
    предохранителе другое» не могло случиться.
    """

    def __init__(self, limit_rub: Decimal, on_pause=None):
        self.limit_rub = limit_rub
        self.spent = Decimal("0")
        self.on_pause = on_pause
        self.tripped = False

    def add(self, cost_rub: Decimal) -> bool:
        """Возвращает True, если лимит только что превышен."""
        self.spent += cost_rub
        if self.tripped or self.spent < self.limit_rub:
            return False
        self.tripped = True
        agent_paused.set(1)
        logger.error(
            "daily cost limit exceeded", extra={"spent": str(self.spent), "limit": str(self.limit_rub)}
        )
        if self.on_pause is not None:
            self.on_pause()
        return True

    def reset(self) -> None:
        self.spent = Decimal("0")
        self.tripped = False
        agent_paused.set(0)
