"""Аварийное переключение на резервного провайдера.

Считает подряд идущие ошибки `complete()` у активного провайдера. После
`max_consecutive_errors` подряд — переключается на резервный (если он
настроен) и пробует тот же запрос ещё раз на нём, прежде чем сдаться. Успешный
ответ (от любого провайдера) сбрасывает счётчик до нуля — единичный сбой не
должен копиться неделями.

Переключение обратно на основного — не автоматическое. Это осознанное
решение: если DeepSeek полежал и ожил, агент не должен молча прыгать обратно
на резервный источник истины о ценах без ведома оператора. Возврат — через
`/provider anthropic` в телеграм-боте.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Awaitable, Callable, Optional, Sequence

from app.agent.providers.base import LLMProvider

logger = logging.getLogger("parmangal.providers.failover")


class FailoverProvider(LLMProvider):
    def __init__(
        self,
        primary: LLMProvider,
        fallback: Optional[LLMProvider] = None,
        max_consecutive_errors: int = 3,
        on_switch: Optional[Callable[[str, str], Awaitable[None]]] = None,
    ):
        self.primary = primary
        self.fallback = fallback
        self.max_consecutive_errors = max_consecutive_errors
        self.on_switch = on_switch
        self._active = primary
        self._consecutive_errors = 0

    @property
    def name(self) -> str:
        return self._active.name

    @property
    def active(self) -> LLMProvider:
        return self._active

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
        try:
            response = await self._active.complete(
                model=model, messages=messages, system=system, tools=tools,
                max_tokens=max_tokens, tool_choice=tool_choice,
            )
        except Exception:
            self._consecutive_errors += 1
            logger.warning(
                "llm provider error #%s on %s",
                self._consecutive_errors, self._active.name,
            )
            if (
                self.fallback is not None
                and self._active is self.primary
                and self._consecutive_errors >= self.max_consecutive_errors
            ):
                await self._switch_to(self.fallback)
                # Один запрос не должен провалиться только потому, что
                # переключение случилось прямо на нём — пробуем сразу же.
                response = await self._active.complete(
                    model=model, messages=messages, system=system, tools=tools,
                    max_tokens=max_tokens, tool_choice=tool_choice,
                )
                self._consecutive_errors = 0
            else:
                raise
        else:
            self._consecutive_errors = 0
        return response

    async def _switch_to(self, provider: LLMProvider) -> None:
        previous = self._active
        self._active = provider
        logger.error("llm provider failover: %s -> %s", previous.name, provider.name)
        if self.on_switch is not None:
            await self.on_switch(previous.name, provider.name)

    def estimate_cost(self, model: str, input_tokens: int, output_tokens: int) -> Decimal:
        return self._active.estimate_cost(model, input_tokens, output_tokens)

    @property
    def supports_prompt_caching(self) -> bool:
        return self._active.supports_prompt_caching
