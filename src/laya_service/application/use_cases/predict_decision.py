"""The generic prediction use case.

Orchestrates a single call to the decision-model port. Note what is *absent*
from this file: no HTTP, no JSON, no Laya, no logging framework. It receives a
DTO, calls a Protocol, and returns a DTO. That is what makes it testable with a
three-line fake and swappable behind any inbound adapter.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from laya_service.application.dto.predict_dto import PredictInput, PredictOutput
from laya_service.domain.ports.decision_model import DecisionModel

__all__ = ["PredictDecisionUseCase"]


class PredictDecisionUseCase:
    """Answer arbitrary questions about an arbitrary state.

    Attributes:
        _model: The decision-model port, injected via the constructor. Stored
            under a private name to signal that collaborators are not part of
            this object's public surface.
    """

    def __init__(self, model: DecisionModel) -> None:
        """Initialize the use case.

        Args:
            model: Any object satisfying the :class:`DecisionModel` port.
        """
        self._model = model

    def execute(self, input: PredictInput) -> PredictOutput:
        """Run inference for the supplied state and questions.

        Args:
            input: The validated prediction request.

        Returns:
            A :class:`PredictOutput` carrying the model's answers plus
            provenance metadata.

        Raises:
            ModelLoadError: Propagated from the adapter when the model is not
                available.
            ModelInferenceError: Propagated from the adapter on inference failure.
        """
        answers: Mapping[str, Any] = self._model.predict(
            state=input.state,
            questions=input.questions,
        )
        return PredictOutput(
            answers=dict(answers),
            backend=self._model.backend,
            model_name=self._model.model_name,
        )
