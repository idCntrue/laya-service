"""Tests for the Laya adapter.

The adapter's whole job is to keep Laya behind a wall, so these tests mock the
``laya`` module at the ``importlib`` boundary and assert on the three things
that matter:

1. **Laziness** -- importing or constructing the adapter must not import Laya or
   load weights. A regression here would make every test in the suite slow and
   every worker start expensive.
2. **Load-once** -- concurrent first calls must trigger exactly one load.
3. **Error normalisation** -- a missing package or a failing model must surface
   as a domain ``ModelLoadError``/``ModelInferenceError``, never as a raw
   ``ImportError`` or a bare exception from deep inside torch.

The fake module mirrors Laya 0.3.5's real surface: the class is ``laya.Agent``
(not ``laya.Laya``), it is constructed with ``model_id_or_path``, and
``predict`` returns a ``{"model", "answers", "usage"}`` envelope.
"""

from __future__ import annotations

import sys
import threading
import types
from collections.abc import Mapping
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from laya_service.domain.exceptions import (
    ModelInferenceError,
    ModelLoadError,
    ModelUnavailableError,
)
from laya_service.domain.ports.decision_model import DecisionModel
from laya_service.infrastructure.model.laya_adapter import LayaDecisionModel

VALID_QUESTIONS = {
    "location_reliable": {"type": "noul", "instructions": "Is localization reliable?"}
}

#: The response envelope Laya 0.3.5 actually returns.
REAL_RESPONSE = {
    "model": "laya-rl-agent",
    "answers": {"location_reliable": {"type": "noul", "noul": 0.87, "confidence": 0.87}},
    "usage": {"input_tokens": 42, "output_tokens": 0},
}


def make_fake_laya_module(agent: Any = None) -> types.ModuleType:
    """Build a stand-in ``laya`` module exposing an ``Agent`` class.

    Args:
        agent: The object ``laya.Agent(**kwargs)`` should return. Defaults to a
            MagicMock configured to answer with the real response envelope.

    Returns:
        A module object exposing an ``Agent`` attribute.
    """
    module = types.ModuleType("laya")
    if agent is None:
        agent = MagicMock()
        agent.predict.return_value = REAL_RESPONSE
        agent.device = "cpu"
    module.Agent = MagicMock(return_value=agent)  # type: ignore[attr-defined]
    return module


class TestPortConformance:
    """The adapter satisfies the port."""

    def test_is_a_decision_model(self) -> None:
        """Structural typing holds without importing anything from the port."""
        assert isinstance(LayaDecisionModel(), DecisionModel)


class TestLaziness:
    """Nothing heavy happens at construction time."""

    def test_construction_does_not_import_laya(self) -> None:
        """Constructing the adapter must not import Laya."""
        with patch.dict(sys.modules, {"laya": None}):
            adapter = LayaDecisionModel(model_name="x", backend="cpu")
            assert adapter.is_ready is False

    def test_construction_does_not_call_the_agent_constructor(self) -> None:
        """The Agent class is not instantiated until the first predict."""
        fake = make_fake_laya_module()
        with patch.dict(sys.modules, {"laya": fake}):
            LayaDecisionModel()
            fake.Agent.assert_not_called()

    def test_is_ready_is_false_before_first_use(self) -> None:
        """An unloaded adapter reports not-ready, which drives /readyz."""
        assert LayaDecisionModel().is_ready is False


class TestLazyLoading:
    """The first call loads the model."""

    def test_first_predict_loads_the_model(self) -> None:
        """The Agent class is instantiated on first predict."""
        fake = make_fake_laya_module()
        with patch.dict(sys.modules, {"laya": fake}):
            adapter = LayaDecisionModel()
            adapter.predict(state={"observation": "x"}, questions=VALID_QUESTIONS)
            fake.Agent.assert_called_once()

    def test_is_ready_true_after_load(self) -> None:
        """Readiness flips once the model is resident."""
        fake = make_fake_laya_module()
        with patch.dict(sys.modules, {"laya": fake}):
            adapter = LayaDecisionModel()
            adapter.predict(state={}, questions=VALID_QUESTIONS)
            assert adapter.is_ready is True

    def test_model_is_loaded_only_once_across_calls(self) -> None:
        """Subsequent calls reuse the loaded model."""
        fake = make_fake_laya_module()
        with patch.dict(sys.modules, {"laya": fake}):
            adapter = LayaDecisionModel()
            for _ in range(5):
                adapter.predict(state={}, questions=VALID_QUESTIONS)
            fake.Agent.assert_called_once()

    def test_concurrent_first_calls_load_exactly_once(self) -> None:
        """The lock prevents a thundering herd of loads."""
        fake = make_fake_laya_module()
        with patch.dict(sys.modules, {"laya": fake}):
            adapter = LayaDecisionModel()
            barrier = threading.Barrier(8)

            def worker() -> None:
                barrier.wait()
                adapter.predict(state={}, questions=VALID_QUESTIONS)

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            fake.Agent.assert_called_once()

    def test_preload_loads_at_construction(self) -> None:
        """With preload=True the load happens eagerly."""
        fake = make_fake_laya_module()
        with patch.dict(sys.modules, {"laya": fake}):
            adapter = LayaDecisionModel(preload=True)
            assert adapter.is_ready is True

    def test_preload_failure_does_not_raise(self) -> None:
        """A failed preload degrades to not-ready instead of killing startup."""
        with patch.dict(sys.modules, {"laya": None}):
            adapter = LayaDecisionModel(preload=True)
            assert adapter.is_ready is False


class TestModelIdResolution:
    """The ``repo:subfolder`` configuration form."""

    def test_reports_configured_name(self) -> None:
        """A configured name is reported before loading."""
        assert LayaDecisionModel(model_name="my-model").model_name == "my-model"

    def test_defaults_to_the_laya_repo(self) -> None:
        """An empty name resolves to the official repository."""
        assert LayaDecisionModel(model_name="").model_name == "convaiinnovations/laya"

    def test_splits_repo_and_subfolder(self) -> None:
        """``repo:subfolder`` is passed through as two arguments to Agent."""
        fake = make_fake_laya_module()
        with patch.dict(sys.modules, {"laya": fake}):
            adapter = LayaDecisionModel(model_name="convaiinnovations/laya:multilingual")
            adapter.predict(state={}, questions=VALID_QUESTIONS)
            kwargs = fake.Agent.call_args.kwargs
            assert kwargs["model_id_or_path"] == "convaiinnovations/laya"
            assert kwargs["subfolder"] == "multilingual"

    def test_omits_subfolder_when_absent(self) -> None:
        """A plain repo id does not pass a subfolder."""
        fake = make_fake_laya_module()
        with patch.dict(sys.modules, {"laya": fake}):
            adapter = LayaDecisionModel(model_name="convaiinnovations/laya")
            adapter.predict(state={}, questions=VALID_QUESTIONS)
            kwargs = fake.Agent.call_args.kwargs
            assert "subfolder" not in kwargs

    def test_backend_is_reported(self) -> None:
        """The backend hint is surfaced for response metadata."""
        assert LayaDecisionModel(backend="cuda").backend == "cuda"

    @pytest.mark.parametrize(
        ("backend", "expected"),
        [("auto", None), ("cpu", "cpu"), ("cuda", "cuda")],
    )
    def test_backend_maps_to_a_device(self, backend: str, expected: str | None) -> None:
        """The backend hint becomes Laya's ``device`` argument."""
        fake = make_fake_laya_module()
        with patch.dict(sys.modules, {"laya": fake}):
            adapter = LayaDecisionModel(backend=backend)
            adapter.predict(state={}, questions=VALID_QUESTIONS)
            kwargs = fake.Agent.call_args.kwargs
            if expected is None:
                assert "device" not in kwargs
            else:
                assert kwargs["device"] == expected


class TestErrorNormalisation:
    """Failures become domain errors, never raw exceptions."""

    def test_missing_package_raises_model_load_error(self) -> None:
        """An absent ``laya`` package is a ModelLoadError."""
        with patch.dict(sys.modules, {"laya": None}):
            adapter = LayaDecisionModel()
            with pytest.raises(ModelLoadError):
                adapter.predict(state={}, questions=VALID_QUESTIONS)

    def test_missing_package_message_is_actionable(self) -> None:
        """The error names the fix."""
        with patch.dict(sys.modules, {"laya": None}):
            adapter = LayaDecisionModel()
            with pytest.raises(ModelLoadError, match="not installed"):
                adapter.predict(state={}, questions=VALID_QUESTIONS)

    def test_module_without_agent_class_raises(self) -> None:
        """A module lacking the Agent class is reported clearly."""
        empty = types.ModuleType("laya")
        with patch.dict(sys.modules, {"laya": empty}):
            adapter = LayaDecisionModel()
            with pytest.raises(ModelLoadError, match="no 'Agent' class"):
                adapter.predict(state={}, questions=VALID_QUESTIONS)

    def test_constructor_failure_raises_model_load_error(self) -> None:
        """A failure inside the Agent constructor is normalised."""
        fake = types.ModuleType("laya")
        fake.Agent = MagicMock(side_effect=RuntimeError("weights corrupt"))  # type: ignore[attr-defined]
        with patch.dict(sys.modules, {"laya": fake}):
            adapter = LayaDecisionModel()
            with pytest.raises(ModelLoadError, match="weights corrupt"):
                adapter.predict(state={}, questions=VALID_QUESTIONS)

    def test_inference_failure_raises_model_inference_error(self) -> None:
        """A failure inside predict is normalised separately from a load failure."""
        agent = MagicMock()
        agent.predict.side_effect = RuntimeError("cuda oom")
        fake = make_fake_laya_module(agent)
        with patch.dict(sys.modules, {"laya": fake}):
            adapter = LayaDecisionModel()
            with pytest.raises(ModelInferenceError, match="cuda oom"):
                adapter.predict(state={}, questions=VALID_QUESTIONS)

    def test_load_failure_is_cached(self) -> None:
        """A broken install fails fast rather than retrying a long download."""
        fake = types.ModuleType("laya")
        constructor = MagicMock(side_effect=RuntimeError("nope"))
        fake.Agent = constructor  # type: ignore[attr-defined]
        with patch.dict(sys.modules, {"laya": fake}):
            adapter = LayaDecisionModel()
            for _ in range(3):
                with pytest.raises(ModelLoadError):
                    adapter.predict(state={}, questions=VALID_QUESTIONS)
            constructor.assert_called_once()

    def test_errors_are_domain_errors(self) -> None:
        """Both error types sit under the domain's ModelUnavailableError."""
        assert issubclass(ModelLoadError, ModelUnavailableError)
        assert issubclass(ModelInferenceError, ModelUnavailableError)


class TestResponseNormalisation:
    """Laya's response envelope is unwrapped."""

    def test_unwraps_the_answers_key(self) -> None:
        """The ``answers`` sub-mapping is what the port promises to return."""
        fake = make_fake_laya_module()
        with patch.dict(sys.modules, {"laya": fake}):
            result = LayaDecisionModel().predict(state={}, questions=VALID_QUESTIONS)
            assert result == {
                "location_reliable": {"type": "noul", "noul": 0.87, "confidence": 0.87}
            }

    def test_bare_answer_mapping_passes_through(self) -> None:
        """A response that is already the answers mapping is accepted."""
        agent = MagicMock()
        agent.predict.return_value = {"q": {"type": "noul", "noul": 0.5}}
        fake = make_fake_laya_module(agent)
        with patch.dict(sys.modules, {"laya": fake}):
            result = LayaDecisionModel().predict(state={}, questions=VALID_QUESTIONS)
            assert result == {"q": {"type": "noul", "noul": 0.5}}

    def test_object_with_answers_attribute_is_unwrapped(self) -> None:
        """A response object exposing .answers is unwrapped."""
        agent = MagicMock()
        agent.predict.return_value = MagicMock(answers={"q": {"noul": 0.2}})
        fake = make_fake_laya_module(agent)
        with patch.dict(sys.modules, {"laya": fake}):
            result = LayaDecisionModel().predict(state={}, questions=VALID_QUESTIONS)
            assert result == {"q": {"noul": 0.2}}

    def test_unrecognised_shape_returns_empty_mapping(self) -> None:
        """An unknown shape degrades to an empty mapping rather than a 500."""
        agent = MagicMock()
        agent.predict.return_value = "unexpected"
        fake = make_fake_laya_module(agent)
        with patch.dict(sys.modules, {"laya": fake}):
            assert LayaDecisionModel().predict(state={}, questions=VALID_QUESTIONS) == {}

    def test_state_and_questions_are_forwarded(self) -> None:
        """The adapter passes the payload through unmodified."""
        agent = MagicMock()
        agent.predict.return_value = REAL_RESPONSE
        fake = make_fake_laya_module(agent)
        state: Mapping[str, Any] = {"observation": "hello"}
        with patch.dict(sys.modules, {"laya": fake}):
            LayaDecisionModel().predict(state=state, questions=VALID_QUESTIONS)
            agent.predict.assert_called_once_with(state=dict(state), questions=VALID_QUESTIONS)
