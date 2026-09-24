# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Note that this project is pre-1.0: the API may change in a minor release, and
`0.x` minor bumps should be treated as potentially breaking.

## [Unreleased]

### Added

- Initial public release.
- `scripts/install_systemd.sh` — renders the systemd template for the local
  machine, resolving the path-escaping and hardening toggles that systemd
  cannot handle itself.
- Documentation and packaging consistency tests that fail the build when the
  docs or `pyproject.toml` drift from the code.
- **OpenAI- and Anthropic-compatible endpoints.** An unmodified official SDK can
  point its `base_url` here and use **tool calling**: the caller describes the
  decisions they want with a tool schema, and the answers arrive as the tool
  call's arguments. `POST /v1/chat/completions`, `POST /v1/messages`, and
  `GET /v1/models`.
  Chat is **not** supported and is rejected with 400. The model classifies; it
  does not generate, and a faked generation would be indistinguishable from a
  real one. Verified against `anthropic` 1.7.0 and `openai` 3.17.0.
- **API key administration** at `/admin/keys` (list, create, get, update,
  revoke). Keys are stored as sha256 hashes in a JSON file; the plaintext is
  returned once, at creation, and is unrecoverable afterwards. The `LAYA_API_KEY`
  from the environment is the bootstrap administrator and is never written to the
  file, so it remains the recovery path.

### Fixed

- **`StructuredLogger` raised `KeyError` on reserved field names.**
  `_logger.info("created", name=...)` — `name` collides with a `LogRecord`
  attribute, and `logging` refuses to overwrite it, so a log call became a 500.
  Reserved names are now prefixed (`x_name`) rather than colliding.
- **The API key store read the wrong settings.** `get_api_key_store()` consulted
  the process-wide settings singleton instead of the instance `create_app` was
  given, so an application built with explicit settings wrote keys to one file
  and read them from another. `get_settings_dep()` now reads
  `app.state.settings`.
- **`FakeDecisionModel` always emitted a `noul` answer.** It ignored the
  question type, so a `choice` or `score` consumer could pass its tests while
  being broken against the real adapter. It now emits the per-type shape Laya
  uses.
- **`pip install` failed with `InvalidConfigError`.** Switching to a PEP 639
  license expression (`license = "Apache-2.0"`) while leaving the matching
  `License :: OSI Approved :: Apache Software License` classifier in place makes
  setuptools refuse to build:

      InvalidConfigError: License classifiers have been superseded by
      license expressions

  This broke every CI job and would have broken every user's install. It went
  unnoticed locally because the package was already installed editable, so
  nothing re-ran the build. Covered now by `tests/unit/test_packaging.py`, which
  parses `pyproject.toml` directly and needs no build or network.
- **`mypy --strict` failed on Python 3.12 only.** The `tomllib` import used
  `try/except ModuleNotFoundError` with a fallback to `tomli`. mypy evaluates
  that against the *target* version: on 3.12 it knows `tomllib` exists, treats
  the except branch as dead, and reports `Name "tomllib" already defined`. A
  `sys.version_info` check is understood on both sides. Verified with
  `mypy --python-version 3.10/3.11/3.12`.
- **`tomli` was an undeclared dependency.** On 3.10 the packaging test needs it;
  pytest and mypy happen to pull it in, so it worked locally and failed in CI
  once the import was reached. Now declared explicitly with a version marker
  rather than relied on transitively.
- **`StructuredLogger` was not subscriptable at runtime on 3.10.** An earlier
  attempt to satisfy `mypy --strict` by writing
  `class StructuredLogger(logging.LoggerAdapter[logging.Logger])` made the
  module fail to import on 3.10 with `TypeError: 'type' object is not
  subscriptable`, because the runtime class is not generic there — only the
  stub is. The generic form is now declared under `TYPE_CHECKING` and bound to
  the plain class at runtime, which is correct under 3.10, 3.11 and 3.12.
- **`mypy` failed on Python 3.12 only, with `No module named 'tomli'`.**
  `[tool.mypy] python_version` was pinned to `"3.10"`, so on a 3.12 runner mypy
  analysed the code as 3.10 — resolving the `tomli` fallback in
  `tests/unit/test_packaging.py` — while `tomli` itself was not installed,
  because its marker was `python_version < '3.11'`. Two changes: the pin is
  removed so mypy follows the interpreter it runs under, and `tomli` is declared
  unconditionally so the analysis and the installed set can never disagree.
  Reproduced by hiding `tomli` and confirmed fixed across
  `mypy --python-version` 3.10, 3.11 and 3.12.
- **The documentation tests failed on a clean checkout.** They called
  `create_app()` with no arguments, so on a machine without a `.env` the
  settings fell back to `HOST=0.0.0.0` with an empty `LAYA_API_KEY` and the
  startup guard refused to build the app. Green locally (where `.env` exists),
  red in CI. Settings are now passed explicitly — the guard is the feature, so
  the test supplies configuration rather than weakening it. Verified by running
  the whole suite from a directory with no `.env`.
- **The CI docs job could not construct the app.** With no `.env` present,
  `create_app()` fell back to the defaults — `HOST=0.0.0.0` with an empty
  `LAYA_API_KEY` — and the startup guard correctly refused. The guard is the
  feature; the job now passes explicit settings.
- **systemd template used shell syntax systemd does not support.**
  `NoNewPrivileges=${VAR:-true}` was rejected with `Failed to parse boolean
  value`, silently falling back to the directive default on every start. The
  values are now substituted at install time by `install_systemd.sh`.
- **`status.sh` could not tell which startup mode was in use.** It read only
  `.run/laya.pid`, so a systemd-managed service looked "not running" and a
  systemd/script conflict (both alive, one failing to bind) was invisible. It
  now detects both modes, reports a CONFLICT explicitly, and surfaces
  `restarts` and `linger` — the two values needed to diagnose unexpected
  restarts.
- **Documentation drift.** The error-code tables omitted `forbidden` and
  `http_error`, and listed `payload_too_large` (413) as reachable when
  `MAX_BODY_BYTES` is not enforced — contradicting
  [SECURITY.md](SECURITY.md#known-limitations). Corrected in both `API.md` and
  `API.en.md`, and now covered by a test.

### Notes

- `MAX_BODY_BYTES` is validated but **not enforced**. Documented in
  [SECURITY.md](SECURITY.md#known-limitations) rather than silently ignored.
- Running `systemctl --user` without `loginctl enable-linger` means the service
  is killed when your last session closes. `make status` now reports this.

## [0.1.0] - 2026-09-22

First working version.

### Added

- **HTTP API**
  - `GET /healthz` — liveness probe, always 200, never touches the model.
  - `GET /readyz` — readiness probe, 503 until the model is resident.
  - `POST /v1/predict` — generic inference over a free-form state with
    `noul` / `choice` / `score` questions.
  - `POST /v1/robot-dog/localization-reliability` — typed endpoint returning a
    reliability verdict plus a machine-readable recommendation.
- **Architecture**
  - Clean Architecture / Hexagonal layout: `domain`, `application`,
    `infrastructure`, `interfaces`, with dependencies pointing strictly inward.
  - `DecisionModel` port as a `typing.Protocol`, so the model is replaceable and
    the test suite runs without PyTorch.
  - Architecture fitness tests that parse module imports with `ast` and fail the
    build on a layering violation.
- **Infrastructure**
  - `LayaDecisionModel` adapter — the only module permitted to import `laya`.
    Lazy loading behind a `threading.Lock`, failure caching, and normalisation
    of every Laya exception into a domain error.
  - `pydantic-settings` configuration with an `lru_cache` singleton and a
    startup guard that refuses an unauthenticated non-loopback bind.
  - Structured JSON logging with `request_id`, `trace_id`, `latency_ms`, and a
    secret-redacting filter.
- **HTTP layer**
  - Bearer-token auth using `hmac.compare_digest`, uniform 401s, and auth
    evaluated before routing.
  - Correlation-ID middleware that sanitises caller-supplied IDs to block CRLF
    header injection and log forging.
  - Access logging that records method, path, status, latency, and request id —
    and deliberately never bodies or the `Authorization` header.
  - A single exception-to-status table mapping domain errors to 400, model
    failures to 503, and everything unhandled to an opaque 500.
- **Operations**
  - `Makefile` covering install, lint, typecheck, test, run, start/stop/status,
    smoke test, and Docker.
  - `scripts/` for start, stop, status, logs, smoke test, and systemd
    installation from a template.
  - Multi-stage Dockerfile: non-root, CPU-only PyTorch, `HEALTHCHECK` via
    `urllib` (no `curl` dependency).
  - systemd user unit with hardening, shipped as a template because systemd's
    path-escaping rules differ per directive.
- **Quality gates**
  - `ruff` (E, F, I, N, UP, B, SIM, RUF) and `ruff format`.
  - `mypy --strict` across source and tests.
  - 485 tests, 90% coverage, with a 70% floor enforced by `fail_under`.
- **Docs**
  - `README.md` — architecture, deployment, operations, limitations.
  - `API.md` — interface reference (Chinese).

### Fixed

Bugs found and corrected during initial development, recorded because each one
is a trap a later change could reintroduce:

- **`noul` answers are probabilities, not booleans.** Laya returns the
  probability that the answer is *true* as a float, with `confidence` as a
  **separate** field. An early implementation read it as a boolean and inverted
  the meaning of every verdict. The two quantities are now handled distinctly
  throughout.
- **`LoggerAdapter` rejects arbitrary keyword arguments.** `logger.error(msg,
  error=...)` raises `TypeError` at the call site, which silently masked every
  adapter error and cascaded into 20 test failures. Replaced with a
  `StructuredLogger` that folds extra keywords into `extra`.
- **Broken dependency-injection graph.** `get_predict_use_case()` called
  `get_decision_model()` directly instead of through `Depends`, so FastAPI's
  `dependency_overrides` never reached it — overrides appeared to work while
  silently using the real model. The composition root now chains through
  `Depends`.
- **Missing answer reported maximum confidence.** When the model returned
  nothing, the fallback confidence computed `max(0.0, 1.0 - 0.0) = 1.0` —
  claiming full confidence in an answer that was never given. Now 0.0.
- **A redundant question could override the verdict.** A model answering every
  question affirmatively could flip a "reliable" verdict to "switch". The
  reliability verdict is now authoritative.
- **Docker build failed on `flit_core`.** The torch install used `--index-url`
  alone, which excludes PyPI and therefore the build backend some wheels
  declare. Added `--extra-index-url`.
- **Docker build failed on missing `README.md`.** `.dockerignore` excluded it
  while `pyproject.toml` requires it as the project readme.

[Unreleased]: https://github.com/idCntrue/laya-service/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/idCntrue/laya-service/releases/tag/v0.1.0
