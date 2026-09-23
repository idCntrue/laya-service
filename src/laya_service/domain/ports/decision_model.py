"""The ``DecisionModel`` port.

This is the seam that makes the whole architecture work. The application layer
depends on *this Protocol* and never on ``laya``, PyTorch, or any concrete
implementation. Swapping Laya for a mock, a fine-tuned model, or a remote HTTP
service is a change confined entirely to the infrastructure layer.

``typing.Protocol`` (rather than ``abc.ABC``) is used so that adapters need not
import anything from this module to satisfy it -- structural typing keeps the
dependency arrow pointing inward. The Protocol is ``runtime_checkable`` so tests
can assert conformance without instantiating.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

__all__ = ["DecisionModel"]


@runtime_checkable
class DecisionModel(Protocol):
    """Port describing a zero-shot decision model.

    Implementations must be safe to call from multiple threads, or must
    serialise access internally. The service runs under a single Uvicorn worker
    with a thread pool, so ``predict`` may be entered concurrently.
    """

    def predict(
        self,
        state: Mapping[str, Any],
        questions: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Answer a set of questions about a given state.

        Args:
            state: Free-form description of the situation, e.g.
                ``{"observation": "..."}``. Must be JSON-serialisable.
            questions: Mapping of ``question_id`` to a question specification of
                the form ``{"type": "noul", "instructions": "..."}``.

        Returns:
            A mapping of ``question_id`` to
            ``{"type": <type>, "<type>": <answer>}``.

        Raises:
            ModelLoadError: If the underlying model could not be loaded.
            ModelInferenceError: If the model failed during inference.
        """
        ...

    @property
    def is_ready(self) -> bool:
        """Whether the model is loaded and able to serve inference.

        Returns:
            ``True`` once the backing model is resident and warm. Readiness
            probes consult this to decide whether to accept traffic.
        """
        ...

    @property
    def backend(self) -> str:
        """Identifier of the active compute backend, e.g. ``"cpu"`` or ``"cuda"``."""
        ...

    @property
    def model_name(self) -> str:
        """Identifier of the loaded model, suitable for response metadata."""
        ...
