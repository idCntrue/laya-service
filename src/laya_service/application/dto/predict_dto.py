"""Data-transfer objects for the generic prediction use case.

DTOs are plain, framework-free data carriers. They exist so that the use case
never sees a Pydantic model, an HTTP request, or a Laya response object -- only
its own vocabulary. The interface layer maps wire formats into these.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from laya_service.domain.exceptions import InvalidQuestionError

__all__ = ["PredictInput", "PredictOutput"]


@dataclass(frozen=True, slots=True)
class PredictInput:
    """Input to the generic prediction use case.

    Attributes:
        state: Free-form description of the situation handed to the model.
        questions: Mapping of ``question_id`` to a question specification of the
            form ``{"type": "noul", "instructions": "..."}``.
    """

    state: Mapping[str, Any]
    questions: Mapping[str, Any]

    def __post_init__(self) -> None:
        """Validate that the request is structurally answerable.

        Raises:
            InvalidQuestionError: If no questions were supplied, or a question
                is missing its ``type``/``instructions`` fields.
        """
        if not self.questions:
            raise InvalidQuestionError("at least one question must be supplied")
        for question_id, spec in self.questions.items():
            if not isinstance(spec, Mapping):
                raise InvalidQuestionError(
                    f"question {question_id!r} must be a mapping, got {type(spec).__name__}"
                )
            if not spec.get("type"):
                raise InvalidQuestionError(f"question {question_id!r} is missing a 'type' field")
            if not str(spec.get("instructions", "")).strip():
                raise InvalidQuestionError(
                    f"question {question_id!r} is missing non-empty 'instructions'"
                )
            # 'choice' and 'score' questions are unanswerable without options.
            if spec["type"] in {"choice", "score"} and not spec.get("criteria"):
                raise InvalidQuestionError(
                    f"question {question_id!r} of type {spec['type']!r} requires 'criteria'"
                )


@dataclass(frozen=True, slots=True)
class PredictOutput:
    """Output of the generic prediction use case.

    Attributes:
        answers: Raw answer mapping keyed by ``question_id``, already in Laya's
            wire shape.
        backend: Compute backend that served the request.
        model_name: Identifier of the model that served the request.
    """

    answers: Mapping[str, Any] = field(default_factory=dict)
    backend: str = "unknown"
    model_name: str = "unknown"
