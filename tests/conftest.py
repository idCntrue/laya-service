"""Shared pytest fixtures.

The centrepiece is :class:`FakeDecisionModel` -- a deterministic implementation
of the ``DecisionModel`` port. Because the application layer depends on the port
rather than on Laya, the entire use-case suite runs in milliseconds with no
model weights, no GPU, and no network. That is the payoff of the architecture,
made concrete.

The fake is also used to drive the integration tests: the FastAPI app is built
with a dependency override that swaps the real adapter for the fake, so the
whole HTTP stack is exercised without downloading anything.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from laya_service.infrastructure.config.settings import Settings
from laya_service.interfaces.http.app import create_app
from laya_service.interfaces.http.dependencies import (
    get_decision_model,
    get_prompt_builder,
)

#: Token used by every authenticated test. Not a real secret.
TEST_API_KEY = "test-key-not-a-real-secret"

__all__ = ["TEST_API_KEY"]


class FakeDecisionModel:
    """A deterministic stand-in for the Laya adapter.

    Models Laya's two distinct per-answer quantities, which are easy to
    conflate and are *not* the same thing:

    * ``probability_true`` -- the probability that a ``noul`` answer is true.
      This is what the service thresholds to decide ``reliable``.
    * ``confidence`` -- the model's confidence in whichever verdict it gave.
      This is what the service compares against the caller's threshold to
      decide ``confident``.

    A model can be 95% confident that the answer is *false*: that is
    ``probability_true=0.05, confidence=0.95``.

    Attributes:
        calls: Every ``(state, questions)`` pair passed to :meth:`predict`.
        probability_true: Probability that each ``noul`` answer is true.
        confidence: The model's confidence in its verdict.
        raise_on_predict: When set, :meth:`predict` raises this instead of
            returning, so failure paths can be tested.
    """

    def __init__(
        self,
        probability_true: float = 0.9,
        confidence: float | None = None,
        *,
        ready: bool = True,
    ) -> None:
        """Initialize the fake.

        Args:
            probability_true: Probability that each ``noul`` answer is true.
            confidence: The model's confidence in its verdict. Defaults to the
                distance of ``probability_true`` from the decision boundary,
                which is what a well-behaved model would report.
            ready: What :attr:`is_ready` should report.
        """
        self.probability_true = probability_true
        self.confidence = (
            confidence if confidence is not None else max(probability_true, 1.0 - probability_true)
        )
        self._ready = ready
        self.calls: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        self.raise_on_predict: Exception | None = None

    def predict(
        self,
        state: Mapping[str, Any],
        questions: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Return a canned answer for every question asked.

        The return value is the *unwrapped* answer mapping the port promises --
        ``{question_id: answer_entry}`` -- not Laya's outer
        ``{"model", "answers", "usage"}`` envelope. Unwrapping is the adapter's
        job, so a fake that returned the envelope would be testing a contract
        the port does not have. The entry shape itself does mirror Laya 0.3.5:
        a ``noul`` answer is a **float probability of true**, not a boolean.

        Args:
            state: The situation description, recorded for assertions.
            questions: The questions, recorded for assertions.

        Returns:
            A mapping of question id to a Laya-shaped answer entry.

        Raises:
            Exception: Whatever was assigned to :attr:`raise_on_predict`.
        """
        self.calls.append((state, questions))
        if self.raise_on_predict is not None:
            raise self.raise_on_predict
        return {
            question_id: {
                "type": spec.get("type", "noul"),
                "noul": round(self.probability_true, 4),
                "confidence": round(self.confidence, 4),
                "action": {"act_probability": 0.0},
            }
            for question_id, spec in questions.items()
        }

    @property
    def is_ready(self) -> bool:
        """Whether the fake reports itself ready."""
        return self._ready

    @property
    def backend(self) -> str:
        """The backend identifier reported in response metadata."""
        return "fake"

    @property
    def model_name(self) -> str:
        """The model identifier reported in response metadata."""
        return "fake-model-v1"


@pytest.fixture
def fake_model() -> FakeDecisionModel:
    """Provide a fresh :class:`FakeDecisionModel`.

    Returns:
        A fake reporting a 0.9 probability of true at 0.9 confidence, which
        reads as a confident "reliable" verdict.
    """
    return FakeDecisionModel()


@pytest.fixture
def settings() -> Settings:
    """Provide settings suitable for tests.

    The host is loopback so that the API-key requirement is not enforced by the
    settings validator, but a key is supplied anyway so the auth middleware is
    active and can be tested.

    Returns:
        A :class:`Settings` instance pinned to test values.
    """
    return Settings(
        host="127.0.0.1",
        port=8000,
        laya_api_key=TEST_API_KEY,
        log_level="WARNING",
        max_body_bytes=65536,
        preload_model=False,
        _env_file=None,  # type: ignore[call-arg]
    )


@pytest.fixture
def app(settings: Settings, fake_model: FakeDecisionModel) -> Iterator[FastAPI]:
    """Provide a FastAPI app with the model dependency overridden.

    Args:
        settings: The test settings.
        fake_model: The fake to inject in place of the Laya adapter.

    Yields:
        A configured application whose decision model is the fake.
    """
    application = create_app(settings=settings)
    application.dependency_overrides[get_decision_model] = lambda: fake_model
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Provide an authenticated test client.

    Args:
        app: The configured test application.

    Yields:
        A :class:`TestClient` that sends the bearer token by default.
    """
    with TestClient(app, headers={"Authorization": f"Bearer {TEST_API_KEY}"}) as test_client:
        yield test_client


@pytest.fixture
def anon_client(app: FastAPI) -> Iterator[TestClient]:
    """Provide an unauthenticated test client.

    Args:
        app: The configured test application.

    Yields:
        A :class:`TestClient` that sends no credentials.
    """
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def prompt_builder():  # type: ignore[no-untyped-def]
    """Provide the real prompt builder.

    Returns:
        A :class:`LocalizationPromptBuilder`. It has no dependencies, so the
        real implementation is used rather than a fake -- testing against a
        double here would prove nothing about the prompt text.
    """
    return get_prompt_builder()
