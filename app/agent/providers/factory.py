"""Сборка провайдера из настроек.

Один вызов `build_provider(settings)` возвращает то, с чем `AgentLoop`
работает не задумываясь: сам провайдер (обычный случай) или
`FailoverProvider`, если в настройках указан резервный.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from app.agent.providers.anthropic_provider import AnthropicProvider
from app.agent.providers.base import LLMProvider
from app.agent.providers import deepseek_provider as _ds
from app.agent.providers.deepseek_provider import DeepSeekProvider
from app.agent.providers.failover import FailoverProvider

MAIN_MODEL = "claude-sonnet-5"
CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"

_DEFAULT_MODELS: dict[str, tuple[str, str]] = {
    # provider_name -> (dialog_model, classifier_model)
    "anthropic": (MAIN_MODEL, CLASSIFIER_MODEL),
    "deepseek": (_ds.DIALOG_MODEL, _ds.CLASSIFIER_MODEL),
}


def default_models_for(provider_name: str) -> tuple[str, str]:
    try:
        return _DEFAULT_MODELS[provider_name]
    except KeyError:
        raise ValueError(f"неизвестный провайдер {provider_name!r}") from None


def resolve_models(settings: Any) -> tuple[str, str]:
    """(dialog_model, classifier_model) с учётом переопределений в настройках.

    LLM_DIALOG_MODEL / LLM_CLASSIFIER_MODEL пустые по умолчанию — тогда
    берутся модели llm_provider'а. Непустое значение переопределяет только
    свою часть пары, не обе сразу."""
    default_dialog, default_classifier = default_models_for(settings.llm_provider)
    return (
        settings.llm_dialog_model or default_dialog,
        settings.llm_classifier_model or default_classifier,
    )


def _build_single(provider_name: str, settings: Any) -> LLMProvider:
    if provider_name == "anthropic":
        return AnthropicProvider(
            api_key=settings.anthropic_api_key.get_secret_value(),
            base_url=settings.llm_base_url or "",
        )
    if provider_name == "deepseek":
        return DeepSeekProvider(
            api_key=settings.deepseek_api_key.get_secret_value(),
            base_url=settings.llm_base_url or "",
            enable_thinking=settings.deepseek_enable_thinking,
        )
    raise ValueError(f"неизвестный провайдер {provider_name!r}")


def build_provider(
    settings: Any,
    on_switch: Optional[Callable[[str, str], Awaitable[None]]] = None,
) -> LLMProvider:
    primary = _build_single(settings.llm_provider, settings)
    fallback_name = getattr(settings, "llm_fallback_provider", None)
    if not fallback_name:
        return primary
    fallback = _build_single(fallback_name, settings)
    return FailoverProvider(
        primary,
        fallback,
        max_consecutive_errors=settings.llm_fallback_after_errors,
        on_switch=on_switch,
    )
