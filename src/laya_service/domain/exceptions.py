"""Domain-level exceptions.

This module is part of the innermost layer of the architecture and therefore
depends on the Python standard library only. No framework, no third-party
package, no I/O.

The hierarchy is deliberately shallow and meaningful:

* ``DomainError``            -- base class for every domain failure.
* ``InvalidProbabilityError``        -- a probability fell outside ``[0, 1]``.
* ``InvalidLocalizationSummaryError``-- a localization summary was incoherent.
* ``InvalidDecisionError``           -- a decision carried unusable data.

Outer layers translate these into application exceptions, which are in turn
translated into HTTP status codes by the interface layer. The domain itself
knows nothing about HTTP.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for all domain-level errors.

    Attributes:
        message: Human-readable description of the failure. Safe to surface to
            an authenticated caller; never contains a stack trace.
        code: Stable, machine-readable identifier used by the interface layer
            to build the error envelope.
    """

    code: str = "domain_error"

    def __init__(self, message: str) -> None:
        """Initialize the error.

        Args:
            message: Human-readable description of the failure.
        """
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        """Return the human-readable message."""
        return self.message


class InvalidProbabilityError(DomainError):
    """Raised when a probability value is outside the closed interval [0, 1]."""

    code = "invalid_probability"


class InvalidLocalizationSummaryError(DomainError):
    """Raised when a localization summary is internally incoherent."""

    code = "invalid_localization_summary"


class InvalidDecisionError(DomainError):
    """Raised when a decision carries structurally unusable data."""

    code = "invalid_decision"


class InvalidQuestionError(DomainError):
    """Raised when a question specification is malformed or unsupported."""

    code = "invalid_question"


class ModelUnavailableError(DomainError):
    """Base class for failures originating in a decision-model adapter.

    Declared in the domain -- not in infrastructure -- so that the application
    layer can catch it without importing any concrete adapter, preserving the
    inward dependency direction.
    """

    code = "model_unavailable"


class ModelLoadError(ModelUnavailableError):
    """Raised when the backing model could not be imported, downloaded, or loaded."""

    code = "model_load_failed"


class ModelInferenceError(ModelUnavailableError):
    """Raised when a loaded model failed while answering a question."""

    code = "model_inference_failed"
