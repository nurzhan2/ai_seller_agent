"""Провайдер DeepSeek через их Anthropic-совместимый эндпоинт.

Факты ниже сверены живыми вызовами `api.deepseek.com/anthropic` 2026-08-20
(не только по документации — см. `docs/PROVIDER_COMPARISON.md`, раздел
«Известные архитектурные различия провайдеров»):

- `base_url="https://api.deepseek.com/anthropic"`, ключ в заголовке
  `x-api-key` — `anthropic.AsyncAnthropic(api_key=..., base_url=...)`
  подключается без единой другой строчки кода.
- `deepseek-v4-pro` / `deepseek-v4-flash` — модели, которые по умолчанию
  думают (`thinking`-блок) перед ответом. При маленьком `max_tokens`
  (наш классификатор просит 8) модель может исчерпать лимит целиком на
  размышлении и вернуть ПУСТОЙ ответ без единого текстового блока —
  воспроизведено: `max_tokens=8` без отключения thinking → `stop_reason
  == "max_tokens"`, `content == [ThinkingBlock(...)]`, текста нет.
  `thinking={"type": "disabled"}` чинит это полностью — с тем же
  `max_tokens=8` результат чистый текстовый блок за 1 токен вывода.
  Отключено здесь по умолчанию: без этого либо ломается классификатор
  (пустой ответ), либо стоимость и задержка основного хода становятся
  непредсказуемо выше Anthropic не из-за качества ответа, а из-за
  скрытого размышления — нечестное сравнение для харнесса из Части 4.
  Включить обратно: `DEEPSEEK_ENABLE_THINKING=true` в `.env`.
- `cache_control` в Anthropic-совместимом режиме DeepSeek сам молча
  игнорирует (подтверждено таблицей совместимости в их доке) — здесь его
  можно передавать не глядя, падать он не будет. Именно поэтому здесь нет
  никакой специальной обработки этого поля: system передаётся как есть.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional, Sequence

from app.agent.providers.base import LLMProvider

BASE_URL = "https://api.deepseek.com/anthropic"

DIALOG_MODEL = "deepseek-v4-pro"
CLASSIFIER_MODEL = "deepseek-v4-flash"

# Рубли за миллион токенов. Источник: api-docs.deepseek.com/quick_start/pricing,
# сверено 2026-08-20 (цены в USD, offline/peak). Взят PEAK-тариф (дороже
# off-peak ровно вдвое) и вариант "cache miss" — консервативная оценка
# сверху, потому что через наш путь (cache_control игнорируется) мы не можем
# гарантированно рассчитывать на автоматическое дисковое кеширование DeepSeek
# для каждого запроса. Курс USD→RUB — тот же ~90, что уже неявно зашит в
# рублёвых ставках Anthropic в этом проекте (см. anthropic_provider.py) —
# взят для сопоставимости, а не заново придуман.
PRICE_PER_MTOK_RUB: dict[str, tuple[Decimal, Decimal]] = {
    DIALOG_MODEL: (Decimal("118.80"), Decimal("356.40")),
    CLASSIFIER_MODEL: (Decimal("39.60"), Decimal("118.80")),
}


class DeepSeekProvider(LLMProvider):
    name = "deepseek"

    def __init__(
        self,
        client: Any = None,
        api_key: str = "",
        base_url: str = "",
        enable_thinking: bool = False,
    ):
        self.enable_thinking = enable_thinking
        if client is not None:
            self.client = client
        else:
            from anthropic import AsyncAnthropic

            self.client = AsyncAnthropic(api_key=api_key, base_url=base_url or BASE_URL)

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
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system is not None:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools
        if tool_choice is not None:
            # ФОРМА ВАЖНА, И РАБОТАЕТ ОНА НЕ ТАК, КАК КАЖЕТСЯ.
            # {"type": "any"} DeepSeek принимает и МОЛЧА ИГНОРИРУЕТ: замер
            # 2026-09-02 — 1 вызов инструмента на 10 ходов, столько же,
            # сколько без него вовсе. Адресное {"type": "tool", "name": ...}
            # вызов включает надёжно (10 из 10), но ИМЯ НЕ ИСПОЛНЯЕТ: из 40
            # принуждений по четырём разным именам заказанный инструмент
            # позвался 5 раз. Для DeepSeek имя — выключатель, а не выбор;
            # разбор целиком — в докстринге app/agent/tool_forcing.py.
            kwargs["tool_choice"] = tool_choice
        if not self.enable_thinking:
            kwargs["thinking"] = {"type": "disabled"}
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
        # cache_control не роняет запрос, но и не даёт подтверждённой скидки
        # через этот эндпоинт — см. docstring модуля.
        return False
