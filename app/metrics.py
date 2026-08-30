"""Метрики Prometheus и дневной лимит расхода.

Лимит здесь, а не в админке, потому что это предохранитель, а не отчёт: при
превышении агент уходит на паузу сам, без участия человека.

Сутки считаются ПО МОСКВЕ — как у суточного лимита исходящих
(app/channels/daily_limit.py) и у лимита уступок: три дневных потолка в
одном проекте, сбрасывающиеся в разное время, — это гарантированная путаница
в разборе инцидента. Страница /admin/costs при этом группирует по дате UTC,
поэтому между 00:00 и 03:00 МСК её сегодняшняя строка и счётчик
предохранителя могут разойтись на ночной хвост — это единственное известное
расхождение, и оно осознанное.
"""

from __future__ import annotations

import logging
from datetime import date as DateType, datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Optional

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from app.channels.daily_limit import MOSCOW_TZ

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

    Считает по одному источнику с /admin/costs — `Message.llm_meta.cost_rub`,
    — чтобы «в отчёте одно, в предохранителе другое» не могло случиться.

    Три свойства, ради которых это класс, а не счётчик в конвейере:

      * `seed()` — расход, потраченный сегодня ДО старта процесса. Без него
        лимит превращался бы в «3000 ₽ с последнего рестарта», а на Railway
        контейнер перезапускается и при каждом деплое (тот же класс ошибки,
        что уже стоил 65 сообщений: состояние, живущее только в памяти
        процесса, молча обнуляется при выкатке);
      * сутки катятся сами, по Москве. Отдельный планировщик для сброса
        не нужен: день проверяется на каждом `add()`;
      * снятая пауза — РУЧНОЕ действие. Полночь обнуляет счётчик, но не
        возвращает агента в работу: /resume нажимает человек, который
        посмотрел, на что ушли деньги. Автовозврат к трате по расписанию —
        последнее, чего ждут от предохранителя.

    `limit_rub <= 0` — осознанно выключенный лимит (см. app/config.py).
    """

    def __init__(
        self,
        limit_rub: Decimal,
        on_pause: Optional[Callable[[], Any]] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
    ):
        self.limit_rub = Decimal(limit_rub)
        self.spent = Decimal("0")
        self.on_pause = on_pause
        self.tripped = False
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._day = self._moscow_day()

    @property
    def enabled(self) -> bool:
        return self.limit_rub > 0

    def _moscow_day(self) -> DateType:
        return self._now_fn().astimezone(MOSCOW_TZ).date()

    def seed(self, spent_rub: Decimal) -> bool:
        """Учесть расход, уже сделанный сегодня до старта процесса.

        Возвращает True, если лимит был исчерпан ещё ДО запуска: тогда
        поднимать агента в рабочем состоянии нельзя, но и слать алерт заново
        не нужно — он ушёл в тот раз, когда лимит был реально превышен.
        Решение о паузе принимает вызывающий код (app/main.py), потому что
        пауза — это состояние приложения, а не метрика.
        """
        self.spent = Decimal(spent_rub)
        self.tripped = self.enabled and self.spent >= self.limit_rub
        agent_paused.set(1 if self.tripped else 0)
        return self.tripped

    def add(self, cost_rub: Decimal) -> bool:
        """Возвращает True, если лимит только что превышен (ровно один раз)."""
        day = self._moscow_day()
        if day != self._day:
            # Полночь по Москве. Счётчик обнуляется, пауза — нет, см. докстринг.
            self._day = day
            self.spent = Decimal("0")
            self.tripped = False
        self.spent += Decimal(cost_rub)
        if not self.enabled or self.tripped or self.spent < self.limit_rub:
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
        self._day = self._moscow_day()
