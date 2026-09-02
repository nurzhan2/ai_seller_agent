"""Абстракция провайдера LLM.

`complete()` обязан возвращать объект в форме ответа Anthropic Messages API
(`.content` — список блоков с `.type`, `.usage` — с `.input_tokens` /
`.output_tokens` / `.cache_read_input_tokens`). Это не искусственное
ограничение интерфейса — DeepSeek со своего Anthropic-совместимого эндпоинта
буквально отдаёт такие объекты через тот же `anthropic` SDK, если указать ему
другой `base_url`. Проверено живым вызовом, не только по документации:
`https://api.deepseek.com/anthropic` действительно возвращает
`anthropic.types.Message` с настоящим `Usage`. Поэтому `AgentLoop` работает с
любым провайдером ни строчки не меняя — весь цикл витков инструментов и вся
логика подсчёта стоимости остаются provider-agnostic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any, Optional, Sequence


class LLMProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def complete(
        self,
        *,
        model: str,
        messages: list[dict],
        system: Any = None,
        tools: Optional[Sequence[dict]] = None,
        max_tokens: int,
        tool_choice: Optional[dict] = None,
    ) -> Any:
        """Один вызов модели. Возвращает Anthropic-форму ответа."""

    def estimate_cost(self, model: str, input_tokens: int, output_tokens: int) -> Decimal:
        return Decimal("0")

    @property
    def supports_prompt_caching(self) -> bool:
        """Понимает ли провайдер `cache_control` и реально экономит на нём.

        Не про то, падает ли он при виде этого поля (падать нельзя ни при
        каком ответе на этот вопрос — см. Часть 3 промта №12), а про то,
        есть ли смысл полагаться на снижение стоимости кешированных токенов.
        """
        return False
