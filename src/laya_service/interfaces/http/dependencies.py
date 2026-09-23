"""Dependency-injection wiring for the HTTP layer.

This is the composition root: the single place where concrete adapters are
constructed and bound to the abstract ports the application layer depends on.
Everything above this file works with interfaces; everything below it is a
detail.

**Why the factories take ``Depends`` arguments rather than calling each other.**
A factory that calls ``get_decision_model()`` directly is invisible to FastAPI's
dependency-override mechanism, so a test (or a deployment) that swaps the model
would silently keep using the real one. Chaining through ``Depends`` makes the
whole graph overridable from a single leaf. That is not a testing convenience --
it is the difference between a composition root that works and one that merely
looks like it does.
"""

from __future__ import annotations

from functools import lru_cache

from fastapi import Depends

from laya_service.application.services.localization_prompt_builder import (
    LocalizationPromptBuilder,
)
from laya_service.application.use_cases.evaluate_localization import (
    EvaluateLocalizationUseCase,
)
from laya_service.application.use_cases.predict_decision import PredictDecisionUseCase
from laya_service.domain.ports.decision_model import DecisionModel
from laya_service.infrastructure.config.settings import Settings, get_settings
from laya_service.infrastructure.model.laya_adapter import LayaDecisionModel

__all__ = [
    "get_decision_model",
    "get_localization_use_case",
    "get_predict_use_case",
    "get_prompt_builder",
    "get_settings_dep",
    "reset_model",
]


@lru_cache(maxsize=1)
def _model_singleton() -> LayaDecisionModel:
    """Build the process-wide decision model.

    Memoised rather than created per request because loading Laya pulls in
    PyTorch and downloads weights. One instance per process, shared across
    threads; the adapter serialises its own initialisation.

    Returns:
        The shared :class:`LayaDecisionModel` adapter.
    """
    settings = get_settings()
    return LayaDecisionModel(
        model_name=settings.laya_model,
        backend=settings.laya_backend,
        cache_dir=settings.model_cache_dir,
        hf_endpoint=settings.hf_endpoint,
        preload=settings.preload_model,
    )


@lru_cache(maxsize=1)
def _prompt_builder_singleton() -> LocalizationPromptBuilder:
    """Build the shared prompt builder.

    Returns:
        The stateless :class:`LocalizationPromptBuilder`.
    """
    return LocalizationPromptBuilder()


def get_settings_dep() -> Settings:
    """FastAPI dependency returning the settings singleton.

    Returns:
        The process-wide settings.
    """
    return get_settings()


def get_decision_model() -> DecisionModel:
    """FastAPI dependency returning the decision-model port.

    Returns:
        The shared model adapter, typed as the port so route code cannot depend
        on Laya specifics.
    """
    return _model_singleton()


def get_prompt_builder() -> LocalizationPromptBuilder:
    """FastAPI dependency returning the prompt builder.

    Returns:
        The shared prompt builder.
    """
    return _prompt_builder_singleton()


def get_predict_use_case(
    model: DecisionModel = Depends(get_decision_model),
) -> PredictDecisionUseCase:
    """FastAPI dependency returning the generic prediction use case.

    Args:
        model: The decision-model port, resolved through the dependency graph so
            that an override of :func:`get_decision_model` reaches here.

    Returns:
        A use case bound to the resolved model.
    """
    return PredictDecisionUseCase(model=model)


def get_localization_use_case(
    model: DecisionModel = Depends(get_decision_model),
    prompt_builder: LocalizationPromptBuilder = Depends(get_prompt_builder),
) -> EvaluateLocalizationUseCase:
    """FastAPI dependency returning the localization use case.

    Args:
        model: The decision-model port, resolved through the dependency graph.
        prompt_builder: The prompt builder, resolved through the graph.

    Returns:
        A use case bound to the resolved collaborators.
    """
    return EvaluateLocalizationUseCase(model=model, prompt_builder=prompt_builder)


def reset_model() -> None:
    """Drop the memoised model and prompt builder.

    Used by tests to inject a fake model between cases, and by the shutdown hook
    to release the adapter's reference so the weights can be collected.
    """
    _model_singleton.cache_clear()
    _prompt_builder_singleton.cache_clear()
