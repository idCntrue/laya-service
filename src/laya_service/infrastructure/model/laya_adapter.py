"""The Laya adapter -- the only module in the codebase that touches ``laya``.

Everything Laya-specific lives behind this file: the dynamic import, the weight
download, device selection, the thread lock, and the translation between Laya's
response shape and a plain mapping. Nothing above this layer imports ``laya``,
``torch``, or ``transformers``, so the model can be replaced by editing exactly
one file.

The adapter targets Laya's real public surface (verified against ``laya`` 0.3.5):

* ``laya.Agent(model_id_or_path, device, token, subfolder)`` constructs the model.
  There is no ``laya.Laya`` class; earlier drafts of this file assumed one.
* ``agent.predict(state, questions)`` is an alias for ``agent.system_one``.
* The response is ``{"model": str, "answers": {...}, "usage": {...}}``.
* Question types are ``choice``, ``score`` and ``noul`` -- see
  :data:`laya.QTYPES`. A ``noul`` answer is a **float probability of "true"**,
  not a boolean, which is why the application layer thresholds it.

Loading is lazy by default. Importing ``laya`` pulls in PyTorch, which costs
seconds and hundreds of megabytes of RSS; doing that at import time would make
the module untestable without a GPU-sized machine.
"""

from __future__ import annotations

import importlib
import threading
import time
from collections.abc import Mapping
from typing import Any, Final

from laya_service.domain.exceptions import ModelInferenceError, ModelLoadError
from laya_service.logging_config import get_logger

__all__ = ["LayaDecisionModel"]

_logger = get_logger(__name__, component="model")

#: Default Hugging Face repository holding the Laya checkpoints.
_DEFAULT_REPO: Final[str] = "convaiinnovations/laya"

#: Backend hints that map onto Laya's ``device`` argument. ``auto`` and any
#: unrecognised value pass ``None``, letting Laya pick (it prefers CUDA when
#: available and falls back to CPU, including on CUDA OOM mid-inference).
_DEVICE_BY_BACKEND: Final[Mapping[str, str | None]] = {
    "auto": None,
    "cpu": "cpu",
    "cuda": "cuda",
}


class LayaDecisionModel:
    """Adapter implementing the :class:`DecisionModel` port over Laya.

    Attributes:
        _model_name: Configured model id, optionally ``"repo:subfolder"``.
        _backend: Configured compute backend hint.
        _cache_dir: Unused directly -- Laya resolves its own cache via
            ``huggingface_hub``, which honours ``HF_HOME``. Retained so the
            adapter's constructor matches the settings it is built from.
        _hf_endpoint: Optional Hugging Face endpoint override.
        _lock: Guards lazy initialisation so concurrent first requests trigger
            exactly one load.
        _model: The loaded ``laya.Agent``, or ``None`` before first use.
        _load_error: The failure captured during a load attempt, if any. Cached
            so a broken install fails fast on every subsequent request instead
            of retrying a multi-minute download.
    """

    def __init__(
        self,
        model_name: str = "",
        backend: str = "auto",
        cache_dir: str = "",
        hf_endpoint: str = "",
        preload: bool = False,
    ) -> None:
        """Initialize the adapter without loading anything.

        Args:
            model_name: Model id, or ``"repo:subfolder"`` to select a bundled
                checkpoint (e.g. ``"convaiinnovations/laya:multilingual"``).
                Empty uses Laya's default English checkpoint.
            backend: Compute backend hint: ``auto``, ``cpu``, or ``cuda``.
            cache_dir: Retained for interface symmetry; Laya resolves its cache
                through ``huggingface_hub``.
            hf_endpoint: Override for the Hugging Face endpoint base URL.
            preload: When ``True``, attempt the load immediately. Failures are
                captured rather than raised, so a preload failure degrades the
                service to "not ready" instead of preventing startup.
        """
        self._model_name = model_name
        self._backend = backend or "auto"
        self._cache_dir = cache_dir
        self._hf_endpoint = hf_endpoint
        self._lock = threading.Lock()
        self._model: Any | None = None
        self._load_error: Exception | None = None
        self._resolved_model_name: str = model_name or _DEFAULT_REPO

        if preload:
            try:
                self._ensure_loaded()
            except ModelLoadError as exc:
                # Startup must not die here: the readiness probe is the right
                # place to report this, and it keeps the process inspectable.
                _logger.error("model preload failed; service will report not-ready", error=str(exc))

    # -- DecisionModel port -------------------------------------------------

    @property
    def is_ready(self) -> bool:
        """Whether the model is loaded and usable.

        Returns:
            ``True`` only once a successful load has completed.
        """
        return self._model is not None and self._load_error is None

    @property
    def backend(self) -> str:
        """The configured compute backend identifier."""
        return self._backend

    @property
    def model_name(self) -> str:
        """The resolved model identifier, suitable for response metadata."""
        return self._resolved_model_name

    def predict(
        self,
        state: Mapping[str, Any],
        questions: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Answer questions about ``state`` using Laya.

        Args:
            state: The situation description, e.g. ``{"observation": "..."}``.
                Laya also accepts a bare string or a list of conversation turns.
            questions: Question specifications keyed by question id, each
                carrying ``type`` (``choice``/``score``/``noul``) and
                ``instructions``.

        Returns:
            A mapping of ``question_id`` to the answer entry Laya produced,
            e.g. ``{"q": {"type": "noul", "noul": 0.87, "confidence": 0.87}}``.

        Raises:
            ModelLoadError: If the Laya package is missing or the weights could
                not be loaded.
            ModelInferenceError: If the loaded model raised during inference.
        """
        model = self._ensure_loaded()
        started = time.perf_counter()
        try:
            raw = model.predict(state=dict(state), questions=dict(questions))
        except Exception as exc:
            _logger.error("laya inference failed", error=str(exc), exc_info=True)
            raise ModelInferenceError(f"model inference failed: {exc}") from exc

        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        answers = _extract_answers(raw)
        _logger.info(
            "inference complete",
            latency_ms=latency_ms,
            question_count=len(questions),
            answered=len(answers),
            model=self._resolved_model_name,
            backend=self._backend,
        )
        return answers

    # -- Loading ------------------------------------------------------------

    def _ensure_loaded(self) -> Any:
        """Load the model on first use, exactly once, thread-safely.

        Returns:
            The loaded ``laya.Agent``.

        Raises:
            ModelLoadError: If the package is missing or loading failed. The
                failure is cached so later callers fail immediately.
        """
        if self._model is not None:
            return self._model
        with self._lock:
            # Re-check under the lock: another thread may have won the race.
            # mypy narrows `_model` to None above and cannot see that another
            # thread may have assigned it while we waited for the lock, so this
            # branch looks unreachable to it. It is not -- it is the entire
            # point of double-checked locking.
            if self._model is not None:
                return self._model  # type: ignore[unreachable]
            if self._load_error is not None:
                raise ModelLoadError(str(self._load_error)) from self._load_error
            try:
                self._model = self._load()
            except Exception as exc:
                self._load_error = exc
                _logger.error("laya model load failed", error=str(exc), exc_info=True)
                raise ModelLoadError(str(exc)) from exc
            return self._model

    def _load(self) -> Any:
        """Import ``laya`` and construct the agent.

        Returns:
            The constructed ``laya.Agent``.

        Raises:
            ModelLoadError: If ``laya`` cannot be imported or constructed.
        """
        repo, subfolder = _split_model_id(self._model_name)
        _logger.info(
            "loading laya model",
            repo=repo,
            subfolder=subfolder or "<default>",
            backend=self._backend,
            note="first call may download weights from the Hugging Face endpoint",
        )
        started = time.perf_counter()

        try:
            laya = importlib.import_module("laya")
        except ImportError as exc:
            raise ModelLoadError(
                "the 'laya' package is not installed; run `make install` to install it"
            ) from exc

        if self._hf_endpoint:
            # huggingface_hub reads HF_ENDPOINT from the environment. Setting it
            # here keeps the concern inside the adapter rather than leaking a
            # global side effect into the application layer.
            import os

            os.environ.setdefault("HF_ENDPOINT", self._hf_endpoint)

        agent_cls = getattr(laya, "Agent", None)
        if agent_cls is None:
            raise ModelLoadError(
                "the installed 'laya' package exposes no 'Agent' class; "
                "check the version against the pinned requirement (laya>=0.3.5)"
            )

        kwargs: dict[str, Any] = {"model_id_or_path": repo}
        device = _DEVICE_BY_BACKEND.get(self._backend, None)
        if device is not None:
            kwargs["device"] = device
        if subfolder:
            kwargs["subfolder"] = subfolder

        model = agent_cls(**kwargs)
        self._resolved_model_name = self._model_name or repo
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        _logger.info(
            "laya model loaded",
            model=self._resolved_model_name,
            device=str(getattr(model, "device", self._backend)),
            latency_ms=latency_ms,
        )
        return model


def _split_model_id(model_name: str) -> tuple[str, str | None]:
    """Split ``"repo:subfolder"`` into its parts.

    Laya bundles several checkpoints in one repository and selects between them
    with a ``subfolder`` argument. Accepting the combined form keeps the
    configuration to a single environment variable.

    Args:
        model_name: A model id, optionally suffixed with ``:subfolder``.

    Returns:
        A ``(repo, subfolder)`` pair. ``subfolder`` is ``None`` when absent.
    """
    if not model_name:
        return _DEFAULT_REPO, None
    repo, separator, subfolder = model_name.partition(":")
    if not separator or not subfolder:
        return repo or _DEFAULT_REPO, None
    return repo, subfolder


def _extract_answers(raw: Any) -> Mapping[str, Any]:
    """Pull the answer mapping out of a Laya response.

    Laya returns ``{"model": str, "answers": {...}, "usage": {...}}``. Earlier
    and hypothetical shapes are tolerated -- a bare mapping of question id to
    answer is passed through, and an object exposing ``.answers`` is unwrapped
    -- so a library change surfaces as a clear log warning rather than a crash.

    Args:
        raw: Whatever ``laya`` returned.

    Returns:
        A plain mapping of question id to answer entry. Falls back to an empty
        mapping rather than raising, so an unrecognised shape surfaces as an
        unconfident answer downstream instead of a 500.
    """
    if isinstance(raw, Mapping):
        nested = raw.get("answers")
        if isinstance(nested, Mapping):
            return dict(nested)
        # No "answers" key: assume the caller already handed us the answers.
        if raw and all(isinstance(value, Mapping) for value in raw.values()):
            return dict(raw)

    answers = getattr(raw, "answers", None)
    if isinstance(answers, Mapping):
        return dict(answers)

    _logger.warning("unrecognised laya response shape", response_type=type(raw).__name__)
    return {}
