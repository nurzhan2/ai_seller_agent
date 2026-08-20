"""Тесты абстракции LLM-провайдера (промт №12): Anthropic/DeepSeek обёртки,
аварийное переключение и сборка из настроек.

Реальных сетевых вызовов здесь нет — фейковый клиент повторяет форму
`anthropic.AsyncAnthropic` (`.messages.create(**kwargs)`), которую отдаёт и
настоящий Anthropic API, и Anthropic-совместимый эндпоинт DeepSeek (проверено
живым вызовом, см. docstring app/agent/providers/deepseek_provider.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from app.agent.providers.anthropic_provider import AnthropicProvider
from app.agent.providers.deepseek_provider import CLASSIFIER_MODEL, DIALOG_MODEL, DeepSeekProvider
from app.agent.providers.factory import build_provider, default_models_for, resolve_models
from app.agent.providers.failover import FailoverProvider
from app.config import Settings


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class Usage:
    input_tokens: int = 10
    output_tokens: int = 5
    cache_read_input_tokens: int = 0


@dataclass
class FakeResponse:
    content: list
    usage: Usage = field(default_factory=Usage)


class FakeMessages:
    def __init__(self, response: FakeResponse = None, error: Exception = None):
        self.response = response or FakeResponse(content=[TextBlock("ok")])
        self.error = error
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeClient:
    def __init__(self, response: FakeResponse = None, error: Exception = None):
        self.messages = FakeMessages(response, error)


# --------------------------------------------------------------------------
# AnthropicProvider — поведение не меняется
# --------------------------------------------------------------------------

async def test_anthropic_provider_omits_tools_when_empty():
    client = FakeClient()
    provider = AnthropicProvider(client=client)
    await provider.complete(model="claude-sonnet-5", messages=[], system="s", max_tokens=8)
    assert "tools" not in client.messages.calls[0]


async def test_anthropic_provider_passes_tools_when_given():
    client = FakeClient()
    provider = AnthropicProvider(client=client)
    await provider.complete(
        model="claude-sonnet-5", messages=[], system="s", tools=[{"name": "x"}], max_tokens=8
    )
    assert client.messages.calls[0]["tools"] == [{"name": "x"}]


def test_anthropic_provider_cost_known_and_unknown_model():
    provider = AnthropicProvider(client=FakeClient())
    assert provider.estimate_cost("claude-sonnet-5", 10_000, 1_000) > 0
    assert provider.estimate_cost("some-future-model", 1_000, 100) == Decimal("0")


def test_anthropic_provider_supports_caching():
    assert AnthropicProvider(client=FakeClient()).supports_prompt_caching is True


# --------------------------------------------------------------------------
# DeepSeekProvider
# --------------------------------------------------------------------------

async def test_deepseek_provider_disables_thinking_by_default():
    """Без этого max_tokens=8 у классификатора съедается размышлением целиком
    и модель возвращает пустой ответ — воспроизведено живым вызовом
    (docstring deepseek_provider.py). thinking=disabled — единственная защита."""
    client = FakeClient()
    provider = DeepSeekProvider(client=client)
    await provider.complete(model=CLASSIFIER_MODEL, messages=[], system="s", max_tokens=8)
    assert client.messages.calls[0]["thinking"] == {"type": "disabled"}


async def test_deepseek_provider_thinking_can_be_re_enabled():
    client = FakeClient()
    provider = DeepSeekProvider(client=client, enable_thinking=True)
    await provider.complete(model=DIALOG_MODEL, messages=[], system="s", max_tokens=8)
    assert "thinking" not in client.messages.calls[0]


async def test_deepseek_provider_passes_cache_control_through_without_crashing():
    """DeepSeek сам молча игнорирует cache_control (подтверждено таблицей
    совместимости их доки) — здесь достаточно не падать на этом поле."""
    client = FakeClient()
    provider = DeepSeekProvider(client=client)
    system = [{"type": "text", "text": "каталог", "cache_control": {"type": "ephemeral"}}]
    await provider.complete(model=DIALOG_MODEL, messages=[], system=system, max_tokens=8)
    assert client.messages.calls[0]["system"] == system


def test_deepseek_provider_cost_known_and_unknown_model():
    provider = DeepSeekProvider(client=FakeClient())
    assert provider.estimate_cost(DIALOG_MODEL, 10_000, 1_000) > 0
    assert provider.estimate_cost("deepseek-vNext", 1_000, 100) == Decimal("0")


def test_deepseek_provider_does_not_claim_caching_support():
    assert DeepSeekProvider(client=FakeClient()).supports_prompt_caching is False


# --------------------------------------------------------------------------
# FailoverProvider
# --------------------------------------------------------------------------

async def test_failover_stays_on_primary_below_threshold():
    primary = AnthropicProvider(client=FakeClient(error=RuntimeError("boom")))
    fallback = AnthropicProvider(client=FakeClient())
    router = FailoverProvider(primary, fallback, max_consecutive_errors=3)

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await router.complete(model="m", messages=[], max_tokens=8)
    assert router.active is primary


async def test_failover_switches_after_threshold_and_retries_on_fallback():
    primary = AnthropicProvider(client=FakeClient(error=RuntimeError("boom")))
    fallback = AnthropicProvider(client=FakeClient())
    switches = []

    async def on_switch(prev, new):
        switches.append((prev, new))

    router = FailoverProvider(primary, fallback, max_consecutive_errors=2, on_switch=on_switch)

    with pytest.raises(RuntimeError):
        await router.complete(model="m", messages=[], max_tokens=8)
    # Второй сбой подряд — переключение и успешный ответ на резерве.
    response = await router.complete(model="m", messages=[], max_tokens=8)

    assert router.active is fallback
    assert switches == [("anthropic", "anthropic")]  # оба провайдера тут AnthropicProvider
    assert response.content[0].text == "ok"


async def test_failover_without_fallback_configured_raises_as_is():
    primary = AnthropicProvider(client=FakeClient(error=RuntimeError("boom")))
    router = FailoverProvider(primary, fallback=None, max_consecutive_errors=1)
    with pytest.raises(RuntimeError):
        await router.complete(model="m", messages=[], max_tokens=8)


async def test_failover_success_resets_error_counter():
    calls = {"n": 0}

    class FailsOnFirstAndThird:
        def __init__(self):
            self.messages = self

        async def create(self, **kwargs):
            calls["n"] += 1
            if calls["n"] in (1, 3):
                raise RuntimeError("boom")
            return FakeResponse(content=[TextBlock("ok")])

    primary = AnthropicProvider(client=FailsOnFirstAndThird())
    fallback = AnthropicProvider(client=FakeClient())
    router = FailoverProvider(primary, fallback, max_consecutive_errors=2)

    with pytest.raises(RuntimeError):
        await router.complete(model="m", messages=[], max_tokens=8)
    await router.complete(model="m", messages=[], max_tokens=8)  # успех сбрасывает счётчик
    with pytest.raises(RuntimeError):
        await router.complete(model="m", messages=[], max_tokens=8)  # снова только 1 подряд
    assert router.active is primary  # порог 2 подряд ни разу не набран


# --------------------------------------------------------------------------
# Сборка из настроек
# --------------------------------------------------------------------------

def test_default_models_for_each_provider():
    assert default_models_for("anthropic") == ("claude-sonnet-5", "claude-haiku-4-5-20251001")
    assert default_models_for("deepseek") == (DIALOG_MODEL, CLASSIFIER_MODEL)
    with pytest.raises(ValueError):
        default_models_for("openai")


def test_resolve_models_uses_defaults_when_unset():
    settings = Settings(llm_provider="deepseek")
    assert resolve_models(settings) == (DIALOG_MODEL, CLASSIFIER_MODEL)


def test_resolve_models_respects_partial_override():
    settings = Settings(llm_provider="anthropic", llm_classifier_model="claude-haiku-custom")
    dialog_model, classifier_model = resolve_models(settings)
    assert dialog_model == "claude-sonnet-5"       # не переопределён
    assert classifier_model == "claude-haiku-custom"  # переопределён


def test_build_provider_plain_anthropic():
    settings = Settings(llm_provider="anthropic", anthropic_api_key="sk-ant-test")
    provider = build_provider(settings)
    assert isinstance(provider, AnthropicProvider)


def test_build_provider_with_fallback_wraps_in_failover():
    settings = Settings(
        llm_provider="anthropic",
        anthropic_api_key="sk-ant-test",
        llm_fallback_provider="deepseek",
        deepseek_api_key="sk-test",
        llm_fallback_after_errors=5,
    )
    provider = build_provider(settings)
    assert isinstance(provider, FailoverProvider)
    assert isinstance(provider.primary, AnthropicProvider)
    assert isinstance(provider.fallback, DeepSeekProvider)
    assert provider.max_consecutive_errors == 5
