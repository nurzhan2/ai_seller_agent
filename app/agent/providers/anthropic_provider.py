"""Обёртка над `anthropic.AsyncAnthropic`.

Поведение не меняется — это тот же вызов, что был в `AgentLoop` до появления
абстракции провайдера, просто вынесенный в отдельный класс. `tools`
передаётся, только если непустой: раньше `classify()` вызывал
`messages.create` вообще без параметра `tools`, и здесь это сохранено, чтобы
не менять форму запроса к Anthropic без причины.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional, Sequence

from app.agent.providers.base import LLMProvider

# Цены за миллион токенов, рубли. Курс подстановки в рубли (реальные ставки
# Anthropic в USD × ~90 ₽/$) уже был зафиксирован раньше в проекте — здесь
# просто перенесён без изменений, чтобы не плодить второй источник истины.
PRICE_PER_MTOK_RUB: dict[str, tuple[Decimal, Decimal]] = {
    "claude-sonnet-5": (Decimal("270"), Decimal("1350")),
    "claude-haiku-4-5-20251001": (Decimal("72"), Decimal("360")),
}


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, client: Any = None, api_key: str = "", base_url: str = ""):
        if client is not None:
            self.client = client
        else:
            from anthropic import AsyncAnthropic

            kwargs: dict[str, Any] = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            self.client = AsyncAnthropic(**kwargs)

    async def complete(
        self,
        *,
        model: str,
        messages: list[dict],
        system: Any = None,
        tools: Optional[Sequence[dict]] = None,
        max_tokens: int,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system is not None:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools
        return await self.client.messages.create(**kwargs)

    def estimate_cost(self, model: str, input_tokens: int, output_tokens: int) -> Decimal:
        rates = PRICE_PER_MTOK_RUB.get(model)
        if rates is None:
            return Decimal("0")
        per_in, per_out = rates
        million = Decimal("1000000")
        cost = (Decimal(input_tokens) / million) * per_in + (
            Decimal(output_tokens) / million
        ) * per_out
        return cost.quantize(Decimal("0.01"))

    @property
    def supports_prompt_caching(self) -> bool:
        return True
