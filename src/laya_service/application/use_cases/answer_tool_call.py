"""The use case behind the OpenAI and Anthropic compatibility endpoints.

Both wire formats reduce to the same operation: a caller describes a set of
decisions with a JSON Schema, and receives the answers as a JSON object. This
use case performs that operation once, so the two adapters differ only in how
they spell the envelope.

It is deliberately thin. The schema compilation lives in the domain, where it
can be tested exhaustively without I/O; the wire formats live in the interface
layer, where they belong. What remains here is orchestration: compile, predict,
render, and report enough metadata that a caller can tell which answers were
derived rather than returned.
"""

from __future__ import annotations

import time

from laya_service.application.dto.compat_dto import CompatRequest, CompatResult
from laya_service.domain.ports.decision_model import DecisionModel
from laya_service.domain.value_objects.decision_schema import compile_schema, render_arguments
from laya_service.logging_config import get_logger

__all__ = ["AnswerToolCallUseCase"]

_logger = get_logger(__name__, component="compat")


class AnswerToolCallUseCase:
    """Answer a tool call by compiling its schema into model questions.

    Attributes:
        _model: The decision-model port.
    """

    def __init__(self, model: DecisionModel) -> None:
        """Initialize the use case.

        Args:
            model: Any object satisfying the :class:`DecisionModel` port.
        """
        self._model = model

    def execute(self, request: CompatRequest) -> CompatResult:
        """Compile the schema, ask the model, and render the answers.

        Args:
            request: The tool call to answer.

        Returns:
            The arguments object plus provenance metadata.

        Raises:
            UnsupportedSchemaError: If the schema has no honest mapping onto
                model questions. Propagated so the route can return 400 with the
                explanation, rather than answering a different question.
            ModelLoadError: Propagated from the adapter when the model is
                unavailable.
            ModelInferenceError: Propagated from the adapter on inference failure.
        """
        started = time.perf_counter()
        compiled = compile_schema(request.schema)

        answers = self._model.predict(state=request.state, questions=compiled.questions)

        arguments = render_arguments(compiled, answers)
        latency_ms = round((time.perf_counter() - started) * 1000, 2)

        _logger.info(
            "compat tool call answered",
            tool=request.tool_name,
            model=self._model.model_name,
            properties=len(compiled.plans),
            answered=len(arguments),
            derived_thresholds=len(compiled.thresholds),
            latency_ms=latency_ms,
        )

        return CompatResult(
            arguments=arguments,
            model_name=self._model.model_name,
            backend=self._model.backend,
            thresholds=compiled.thresholds,
            latency_ms=latency_ms,
        )
