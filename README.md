# Laya Service

[![CI](https://github.com/idCntrue/laya-service/actions/workflows/ci.yml/badge.svg)](https://github.com/idCntrue/laya-service/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue.svg)](pyproject.toml)

A production HTTP service wrapping the [Laya](https://huggingface.co/convaiinnovations/laya)
non-autoregressive decision engine, built as a Clean Architecture / Hexagonal
application.

The model reads English text and answers typed questions about it — a `noul`
question returns the probability that the answer is true, `choice` returns the
winning option, `score` returns an ordinal rating — all in a single forward
pass. It does **not** generate text, and its zero-shot probabilities are **not**
calibrated. This service exposes that capability over HTTP with authentication,
structured logging, health probes, graceful shutdown, and a dependency graph
that keeps Laya replaceable.

> **中文版：** [README.zh-CN.md](README.zh-CN.md). Both language versions are kept
> in sync; if they disagree, the English one describes the code and the Chinese
> one is a bug.

---

## Table of contents

- [Architecture](#architecture)
- [Layers and the dependency rule](#layers-and-the-dependency-rule)
- [Directory structure](#directory-structure)
- [Installation](#installation)
- [Running the service](#running-the-service)
- [Testing and quality gates](#testing-and-quality-gates)
- [API reference](#api-reference)
- [Calling the service](#calling-the-service)
- [API.md](API.md) — interface reference (Chinese)
- [API.en.md](API.en.md) — interface reference (English)
- [CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) · [CHANGELOG.md](CHANGELOG.md)
- [Configuration](#configuration)
- [Docker deployment](#docker-deployment)
- [systemd deployment](#systemd-deployment)
- [Observability](#observability)
- [Troubleshooting](#troubleshooting)
- [Limitations you must not ignore](#limitations-you-must-not-ignore)
- [Security](#security)

---

## Architecture

```
                          ┌─────────────────────────────────────────┐
   HTTP request           │            interfaces (inbound)         │
   ──────────────────────▶│  FastAPI app, middleware, routes,       │
                          │  schemas, exception handlers            │
                          └────────────────────┬────────────────────┘
                                               │ depends on
                                               ▼
                          ┌─────────────────────────────────────────┐
                          │            application                  │
                          │  use cases, DTOs, prompt builder        │
                          └────────────────────┬────────────────────┘
                                               │ depends on
                                               ▼
                          ┌─────────────────────────────────────────┐
                          │              domain                     │
                          │  entities, value objects, ports,        │
                          │  exceptions   —   ZERO dependencies     │
                          └────────────────────▲────────────────────┘
                                               │ implements
                          ┌────────────────────┴────────────────────┐
                          │           infrastructure (outbound)     │
                          │  LayaDecisionModel adapter, Settings    │
                          └─────────────────────────────────────────┘
```

The arrow into `domain` from `infrastructure` is an *implementation* arrow, not
a dependency arrow: `domain` declares the `DecisionModel` port and knows nothing
about who satisfies it. That inversion is what lets the entire test suite run
without PyTorch.

### Request flow

```
POST /v1/robot-dog/localization-reliability
  │
  ├─ RequestIdMiddleware      assigns/propagates X-Request-ID
  ├─ AccessLogMiddleware      times the request
  ├─ AuthMiddleware           verifies the bearer token
  ├─ CORS                     (only when CORS_ORIGINS is set)
  │
  ├─ routes/robot_dog.py      Pydantic schema → domain value objects
  │     └─ LocalizationSummary(...)   ← validates coherence
  │
  ├─ EvaluateLocalizationUseCase.execute()
  │     ├─ LocalizationPromptBuilder  → English state + questions
  │     └─ DecisionModel.predict()    → the port
  │            └─ LayaDecisionModel   → importlib → laya → torch
  │
  ├─ routes/robot_dog.py      domain result → Pydantic response
  └─ exception_handlers.py    domain error → HTTP status (if any)
```

---

## Layers and the dependency rule

| Layer | May import | Must never import |
|---|---|---|
| `domain` | stdlib only | everything else — no FastAPI, no Pydantic, no Laya |
| `application` | `domain`, stdlib | `infrastructure`, `interfaces`, any framework |
| `infrastructure` | `domain`, `application`, frameworks | `interfaces` |
| `interfaces` | everything inward | — |

These are not conventions. `tests/unit/test_architecture.py` parses every
module's imports with `ast` and fails the build on a violation. If you add a
`import fastapi` to a domain file, `make test` goes red.

**Where each concern lives:**

- **Business rules** (what "unreliable" means, which recommendation to emit,
  what to do when the model is unsure) → `application/use_cases/`
- **Invariants** (a probability is in `[0, 1]`; a jump is non-negative) →
  `domain/value_objects/`
- **Laya, torch, HTTP, env vars** → `infrastructure/` and `interfaces/`

---

## Directory structure

```
.
├── .env.example              Configuration template — copy to .env
├── .editorconfig             Editor consistency
├── .pre-commit-config.yaml   ruff, ruff-format, mypy, whitespace hooks
├── Makefile                  All developer and operator entry points
├── pyproject.toml            Packaging, ruff, mypy, pytest, coverage config
├── README.md                 English (this file)
├── README.zh-CN.md           Chinese
├── API.md                    Interface reference (Chinese)
├── API.en.md                 Interface reference (English)
├── CHANGELOG.md              Release history
├── CONTRIBUTING.md           How to contribute
├── SECURITY.md               Security policy and threat model
├── CODE_OF_CONDUCT.md
├── LICENSE                   Apache-2.0
├── requirements/
│   ├── base.txt              Runtime dependencies
│   ├── dev.txt               base + test/lint tooling
│   └── prod.txt              base + gunicorn
├── src/laya_service/
│   ├── __init__.py           __version__
│   ├── main.py               Entry point → uvicorn.run(factory=True)
│   ├── logging_config.py     JSON formatter, StructuredLogger, secret redaction
│   ├── domain/               ← zero dependencies
│   │   ├── entities/decision.py          Decision, Prediction
│   │   ├── value_objects/
│   │   │   ├── probability.py            Probability (validated [0,1])
│   │   │   └── localization_summary.py   LocalizationSummary
│   │   ├── ports/decision_model.py       DecisionModel Protocol
│   │   └── exceptions.py                 DomainError hierarchy
│   ├── application/          ← depends only on domain
│   │   ├── dto/                          PredictInput/Output, LocalizationInput/Output
│   │   ├── use_cases/                    PredictDecision, EvaluateLocalization
│   │   └── services/                     LocalizationPromptBuilder
│   ├── infrastructure/       ← adapters
│   │   ├── config/settings.py            pydantic-settings + lru_cache singleton
│   │   └── model/laya_adapter.py         the ONLY module that imports laya
│   └── interfaces/http/      ← inbound adapter
│       ├── app.py                        create_app() factory
│       ├── dependencies.py               DI composition root
│       ├── exception_handlers.py         exception → status code table
│       ├── middleware/                   auth, request_id, access_log
│       ├── routes/                       health, predict, robot_dog
│       └── schemas/                      Pydantic request/response models
├── tests/
│   ├── conftest.py           FakeDecisionModel, TestClient fixtures
│   ├── unit/                 domain, application, infrastructure, architecture
│   └── integration/test_api.py
├── scripts/                  start/stop/status/logs/smoke_test/install_systemd
├── deploy/
│   ├── docker/Dockerfile     Multi-stage, non-root
│   └── systemd/
│       └── laya-service.service.in   Template (run install_systemd.sh)
└── .github/
    ├── workflows/ci.yml      lint + typecheck + test + docker
    ├── ISSUE_TEMPLATE/
    ├── PULL_REQUEST_TEMPLATE.md
    └── dependabot.yml
```

---

## Installation

### Prerequisites

- Python **3.10+** (3.10.12 verified)
- ~3 GB free disk for the virtualenv, plus model weights on first inference
- No GPU required. `laya-mlx` is **Apple Silicon only** — do not install it on
  x86_64 Linux.

### One command

```bash
make install-dev
```

This creates `.venv`, installs CPU-only PyTorch, then installs the project and
its dev dependencies.

### Why CPU-only PyTorch matters

`laya` depends on `torch>=2.0.0`. On a machine with no NVIDIA GPU, a plain
`pip install torch` still downloads the **CUDA build (~2.5 GB)** plus a dozen
`nvidia-*` wheels that can never be loaded. `make install-torch` installs
`torch==2.9.1+cpu` (~176 MB) first, so the resolver never considers the CUDA
build.

If you install manually, do this order:

```bash
python3 -m venv .venv
.venv/bin/pip install torch==2.9.1+cpu --index-url https://download.pytorch.org/whl/cpu
.venv/bin/pip install -e ".[dev]"
```

### If `python3 -m venv` fails with "ensurepip is not available"

Some minimal Debian/Ubuntu images ship `python3` without `python3-venv`. The
Makefile handles this: `make venv` creates the environment with
`--without-pip` and bootstraps pip via `get-pip.py`. No `sudo` needed.

---

## Running the service

```bash
# Generate a secret (required before binding a public interface)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# put it in .env as LAYA_API_KEY=...

make start      # background, writes .run/laya.pid and logs/laya.log
make status     # how it is running, process, port, /healthz, /readyz
make logs       # tail the JSON log
make stop       # SIGTERM, escalating to SIGKILL after 30s
make restart
```

Foreground with autoreload for development:

```bash
make run
```

### The first request is slow

The model loads lazily. The first call to `/v1/predict` imports PyTorch and
downloads weights — **several minutes** on a cold cache, and a few GB of disk.

```
/readyz  →  503 not_ready   (before the first request)
/readyz  →  200 ready       (after the model is warm)
```

If you would rather pay that cost at startup than on the first request:

```bash
PRELOAD_MODEL=true
```

**Do not wire a liveness probe to `/readyz`.** The container would be restarted
mid-download, forever, and never become ready. `/healthz` is the liveness probe;
`/readyz` is for load-balancer membership.

---

## Testing and quality gates

```bash
make lint          # ruff check
make format        # ruff check --fix && ruff format
make typecheck     # mypy --strict
make test          # pytest
make test-cov      # pytest --cov, fails under 70%
make check         # lint + format-check + typecheck + test
make smoke         # curl the three endpoints of a running service
```

`mypy --strict` and a 70% coverage floor are both enforced — `make test-cov`
exits non-zero below the threshold.

The test suite is fast because `FakeDecisionModel` satisfies the `DecisionModel`
port. No weights, no network, no GPU.

> **Changing `pyproject.toml`? Re-run the install.** `make test` does not verify
> that the project can be installed — it imports from the editable checkout that
> was already built. A metadata error is invisible until something rebuilds:
>
> ```bash
> .venv/bin/pip install -e ".[dev]"
> ```
>
> This is not hypothetical: a PEP 639 license expression combined with a
> leftover `License ::` classifier passed all 308 tests locally while breaking
> every CI job. See [CONTRIBUTING.md](CONTRIBUTING.md#quality-gates).

---

## API reference

Base URL: `http://<host>:<port>`

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /healthz` | — | Liveness probe |
| `GET /readyz` | — | Readiness probe |
| `POST /v1/predict` | yes | Generic inference |
| `POST /v1/robot-dog/localization-reliability` | yes | Typed reliability verdict |
| `POST /v1/chat/completions` | yes | OpenAI-compatible **tool calling** |
| `POST /v1/messages` | yes | Anthropic-compatible **tool use** |
| `GET /v1/models` | yes | Served model identifiers |
| `/admin/keys` | **admin** | API key administration |

### Authentication

Every `/v1/*` route requires:

```
Authorization: Bearer <LAYA_API_KEY>
```

The Anthropic-style `x-api-key` header is accepted too, so an unmodified
Anthropic SDK authenticates without extra headers.

`/healthz`, `/readyz`, `/docs`, `/redoc`, and `/openapi.json` are public.
Everything under `/admin/` additionally requires a key with the `admin` scope.

### Compatibility layer

`POST /v1/chat/completions` and `POST /v1/messages` let an unmodified official
SDK point its `base_url` here.

**What they can do is tool calling, not chat.** The model behind this service
classifies; it does not generate text. You describe the decisions you want with
a tool's JSON Schema and receive them as the tool call's arguments. A request
that expects prose is rejected with 400 rather than answered with something that
looks like a reply — a faked generation is indistinguishable from a real one.

```python
from anthropic import Anthropic

client = Anthropic(base_url="http://127.0.0.1:9800", api_key="laya_sk_...")

message = client.messages.create(
    model="english",
    max_tokens=1024,                       # required by the SDK; ignored here
    messages=[{"role": "user", "content": "Order #12345 arrived broken"}],
    tools=[{
        "name": "classify_ticket",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["refund", "technical", "billing"],
                    "description": "Which team should handle this?",
                },
            },
        },
    }],
    tool_choice={"type": "tool", "name": "classify_ticket"},
)

print(message.content[0].input)   # {'category': 'refund'}
```

See [API.md](API.md#兼容层openai--anthropic) for the full mapping rules, the
`x-laya-threshold` extension, and the fields that are ignored or rejected.

### Error envelope

Every failure — 400, 401, 404, 422, 500, 503 — returns the same shape:

```json
{
  "ok": false,
  "error": {
    "code": "unauthorized",
    "message": "a valid bearer token is required"
  },
  "request_id": "9f2c1e4b8a7d4f3e9c1b2a3d4e5f6a7b"
}
```

| Code | Status | Meaning |
|---|---|---|
| `unauthorized` | 401 | Missing, malformed, or wrong bearer token |
| `not_found` | 404 | No such route |
| `method_not_allowed` | 405 | Wrong HTTP verb |
| `validation_error` | 422 | Request body failed schema validation |
| `invalid_probability` | 400 | A probability fell outside `[0, 1]` |
| `invalid_localization_summary` | 400 | Summary is internally incoherent |
| `invalid_question` | 400 | Malformed or unsupported question |
| `invalid_decision` | 400 | Decision carried unusable data |
| `model_load_failed` | 503 | Model could not be imported or loaded |
| `model_inference_failed` | 503 | Model raised during inference |
| `internal_error` | 500 | Unhandled — quote `request_id` in a bug report |

`500` never contains a stack trace, a file path, or an internal identifier. The
traceback goes to the log, correlated by `request_id`.

---

### `GET /healthz` — liveness

Always `200` while the process is alive. Never touches the model.

```bash
curl http://127.0.0.1:9800/healthz
```

```json
{ "status": "ok", "version": "0.1.0" }
```

### `GET /readyz` — readiness

`200` once the model is warm, `503` before that.

```json
{
  "status": "not_ready",
  "model_loaded": false,
  "backend": "auto",
  "model": "laya-default",
  "detail": "the decision model has not been loaded yet; it loads on first request..."
}
```

### `POST /v1/predict`

**Request**

| Field | Type | Required | Description |
|---|---|---|---|
| `state` | object | yes | Free-form situation description |
| `questions` | object | yes | Map of question id → `{type, instructions}` |

Question `type` must be one of:

| Type | Answer shape | Requires |
|---|---|---|
| `noul` | float — the **probability that the answer is true** | — |
| `choice` | string — the winning option key | `criteria` (mapping of key → description) |
| `score` | float — expected value of the ordinal rating | `criteria` (list, lowest first) |

> **`noul` does not return a boolean.** This trips people up. Laya returns the
> probability of "true" as a float, alongside a separate `confidence` field:
>
> ```json
> "location_reliable": { "type": "noul", "noul": 0.2281, "confidence": 0.7719 }
> ```
>
> `noul` is the *answer*; `confidence` is how sure the model is of it. A model
> can be 95% confident that the answer is **false** — that is
> `{"noul": 0.05, "confidence": 0.95}`. The two are not interchangeable, and
> conflating them is the most likely way to misuse this service.

```bash
curl -X POST http://127.0.0.1:9800/v1/predict \
  -H "Authorization: Bearer $LAYA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "state": {
      "observation": "Over the past 3 seconds, the x coordinate jumped discontinuously by about 2.3 meters, y was stable, heading reversed three times, and positioning confidence dropped from 0.9 to 0.4. The robot is indoors with weak GPS."
    },
    "questions": {
      "location_reliable": {
        "type": "noul",
        "instructions": "Is the current localization reliable?"
      }
    }
  }'
```

**Response**

```json
{
  "ok": true,
  "data": {
    "answers": {
      "location_reliable": { "type": "noul", "noul": true }
    }
  },
  "meta": {
    "backend": "cpu",
    "model": "laya-default",
    "request_id": "9f2c1e4b8a7d4f3e9c1b2a3d4e5f6a7b",
    "latency_ms": 1843.21
  }
}
```

### `POST /v1/robot-dog/localization-reliability`

The typed, opinionated endpoint. Use this rather than `/v1/predict` when the
question is specifically "can I trust this pose estimate?".

**Request**

| Field | Type | Range | Description |
|---|---|---|---|
| `x_jump_m` | float | `0 … 1000` | Discontinuous jump along x, metres |
| `y_stable` | bool | — | Whether y stayed stable |
| `heading_reversals` | int | `0 … 1000` | Heading reversals in the window |
| `confidence_start` | float | `0 … 1` | Estimator confidence at window start |
| `confidence_end` | float | `0 … 1` | Estimator confidence at window end |
| `environment` | string | 1–128 chars | e.g. `indoor_weak_gps` |
| `confidence_threshold` | float | `0 … 1`, default `0.5` | Bar for `confident` |

Known environment tags: `indoor_weak_gps`, `indoor_strong_gps`,
`outdoor_open_sky`, `outdoor_urban_canyon`, `underground`, `indoor`, `outdoor`.
Unknown tags are accepted and rendered readably.

```bash
curl -X POST http://127.0.0.1:9800/v1/robot-dog/localization-reliability \
  -H "Authorization: Bearer $LAYA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "x_jump_m": 2.3,
    "y_stable": true,
    "heading_reversals": 3,
    "confidence_start": 0.9,
    "confidence_end": 0.4,
    "environment": "indoor_weak_gps"
  }'
```

**Response**

```json
{
  "ok": true,
  "data": {
    "reliable": false,
    "reliability": 0.87,
    "recommendation": "switch_to_visual_imu",
    "confident": true
  },
  "meta": {
    "backend": "cpu",
    "model": "laya-default",
    "request_id": "3c7d9e1f5a2b4c6d8e0f1a2b3c4d5e6f",
    "latency_ms": 1912.44
  }
}
```

**Output fields**

| Field | Description |
|---|---|
| `reliable` | The judgement, derived by thresholding the `noul` probability at 0.5 |
| `reliability` | The model's **confidence in that judgement**, `[0, 1]` |
| `recommendation` | `continue_with_current_estimator` or `switch_to_visual_imu` |
| `confident` | Whether `reliability` cleared `confidence_threshold` |

Note the division of labour: `reliable` comes from the *answer* (`noul`),
`reliability` from the model's *confidence* in it. They are different quantities
and the service keeps them separate deliberately.

**How `recommendation` is chosen** (this is business policy, in the application
layer — not in the route):

1. If `confident` is `false` → `switch_to_visual_imu`. **When the model is
   unsure, the service always recommends the conservative action.** A robot that
   unnecessarily falls back to visual-inertial odometry is inconvenienced; one
   that trusts a bad pose estimate can collide with something.
2. Else if `reliable` is `false` → `switch_to_visual_imu`
3. Else → `continue_with_current_estimator`

---

## Calling the service

### curl

```bash
source .env

curl -sS -X POST http://127.0.0.1:9800/v1/robot-dog/localization-reliability \
  -H "Authorization: Bearer $LAYA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"x_jump_m":2.3,"y_stable":true,"heading_reversals":3,
       "confidence_start":0.9,"confidence_end":0.4,
       "environment":"indoor_weak_gps"}' | python3 -m json.tool
```

### Python

```python
import os
import httpx

BASE = "http://127.0.0.1:9800"
KEY = os.environ["LAYA_API_KEY"]
HEADERS = {"Authorization": f"Bearer {KEY}"}

with httpx.Client(base_url=BASE, headers=HEADERS, timeout=600.0) as client:
    response = client.post(
        "/v1/robot-dog/localization-reliability",
        json={
            "x_jump_m": 2.3,
            "y_stable": True,
            "heading_reversals": 3,
            "confidence_start": 0.9,
            "confidence_end": 0.4,
            "environment": "indoor_weak_gps",
        },
    )
    response.raise_for_status()
    body = response.json()

data = body["data"]
if not data["confident"]:
    # The model was unsure. Do not act on `reliable`; fall back.
    fallback()
elif data["reliable"]:
    trust_the_estimate()
else:
    switch_to_visual_imu()
```

Note the `timeout=600` — the first call downloads model weights.

---

## Configuration

All configuration is read from the environment and `.env` by
`pydantic_settings`. Nothing is hard-coded. Copy `.env.example` to `.env`.

| Variable | Default | Description |
|---|---|---|
| `LAYA_BACKEND` | `auto` | `auto` \| `cpu` \| `cuda` \| `mlx` |
| `LAYA_MODEL` | *(empty)* | Model id; empty uses Laya's default |
| `HF_ENDPOINT` | `https://hf-mirror.com` | Weight download endpoint |
| `HOST` | `0.0.0.0` | Bind interface |
| `PORT` | `9800` | Bind port |
| `LAYA_API_KEY` | *(empty)* | Bearer token. **Required** unless `HOST` is loopback |
| `LOG_LEVEL` | `INFO` | `DEBUG`…`CRITICAL` |
| `CORS_ORIGINS` | *(empty)* | Comma-separated allow-list; empty disables CORS |
| `MAX_BODY_BYTES` | `65536` | Max request body (ceiling 10 MiB) |
| `PRELOAD_MODEL` | `false` | Load weights at startup |
| `MODEL_CACHE_DIR` | *(empty)* | Override the weight cache location |
| `REQUEST_TIMEOUT_S` | `120` | Per-request inference budget |
| `HF_HUB_DISABLE_XET` | `1` in this deployment | Disable the Xet CAS transfer protocol (see Troubleshooting) |

`SYSTEMD_NO_NEW_PRIVILEGES`, `SYSTEMD_PRIVATE_DEVICES` and
`SYSTEMD_PROTECT_KERNEL_MODULES` are read by the **systemd unit**, not by the
application. See [systemd deployment](#systemd-deployment).

### The API-key rule

If `HOST` is **not** `127.0.0.1` / `localhost` / `::1` **and** `LAYA_API_KEY` is
empty, the service **refuses to start**:

```
configuration error: HOST='0.0.0.0' is not a loopback address but LAYA_API_KEY is empty.
Set LAYA_API_KEY, or bind HOST=127.0.0.1 to run without authentication.
```

This is deliberate. An unauthenticated inference endpoint bound to a routable
interface will happily consume all available CPU for anyone who finds it.

---

## Docker deployment

```bash
make docker-build     # multi-stage, non-root, CPU-only torch
make docker-run       # reads .env, maps PORT
make docker-stop
```

The image runs as uid 10001 (non-root), sets `PYTHONUNBUFFERED=1` and
`PYTHONDONTWRITEBYTECODE=1`, exposes 8000 (inside the container — map it to
whatever you like on the host), and has a `HEALTHCHECK` against
`/healthz` (liveness, not readiness — see the note above).

**Mount a volume for the model cache**, or every container restart re-downloads
several GB:

```bash
docker run -d --name laya-service \
  --env-file .env \
  -p 9800:8000 \
  -v laya-hf-cache:/data/hf \
  --restart unless-stopped \
  laya-service:latest
```

For a CUDA build, override the torch index at build time:

```bash
docker build -f deploy/docker/Dockerfile \
  --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu121 \
  -t laya-service:cuda .
```

### Notes on the image

Two things about the Dockerfile are deliberate and worth not "tidying away":

- **The torch install uses `--extra-index-url`, not just `--index-url`.** The
  torch wheel index carries wheels but no source distributions, so with
  `--index-url` alone pip cannot fetch the build backend (`flit_core`) that some
  wheels declare, and the build dies with
  `Could not find a version that satisfies the requirement flit_core`. PyPI is
  kept as a fallback index; the explicit `==2.9.1+cpu` pin still prevents it
  from supplying a CUDA torch.
- **The runtime stage installs only `libgomp1`, and the `HEALTHCHECK` uses
  `urllib` rather than `curl`.** `libgomp1` is genuinely required — torch's
  `libtorch_cpu.so` links `libgomp.so.1`, and without it the container starts
  and then dies on `import torch`. `curl` is not required, and dropping it also
  avoids the Debian apt index, which on a slow mirror dominates build time.

  If you add a dependency that needs to compile from source, add
  `build-essential` to the **builder** stage (it is discarded with that stage).

---

## systemd deployment

A user-level unit is provided at `deploy/systemd/laya-service.service`.

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/laya-service.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now laya-service

systemctl --user status laya-service
journalctl --user -u laya-service -f
systemctl --user restart laya-service
systemctl --user stop laya-service
```

To keep the service running after you log out:

```bash
sudo loginctl enable-linger "$USER"
```

> **If your install path contains a space.** systemd handles it differently per
> directive — quoting works in `ExecStart` and `ReadWritePaths` but not in
> `WorkingDirectory`, and a bare space in `ReadWritePaths` is silently split
> into two invalid paths. `scripts/install_systemd.sh` handles all three cases;
> see the escaping table in [systemd deployment](#systemd-deployment).

The unit includes `Restart=on-failure`, `RestartSec=5s` and `StartLimitBurst=5`
(so a configuration error does not become a hot restart loop), plus hardening:
`NoNewPrivileges`, `PrivateTmp`, `PrivateDevices`, `ProtectSystem=strict`,
`ProtectHome=read-only`, `ProtectKernelTunables`, `ProtectKernelModules`,
`ProtectControlGroups`, `RestrictRealtime`, `RestrictSUIDSGID`, `LockPersonality`.

### If the unit refuses to start with `status=218/CAPABILITIES`

On a **user** systemd manager inside a container or a restricted VM, three of
those directives cannot be applied, because the manager cannot obtain the
capability they need:

| Directive | Needs | Symptom |
|---|---|---|
| `NoNewPrivileges=true` | `CAP_SETPCAP` | `Failed at step CAPABILITIES ... Operation not permitted` |
| `PrivateDevices=true` | `CAP_SYS_ADMIN` | same |
| `ProtectKernelModules=true` | `CAP_SYS_ADMIN` | same |

Each fails **independently**, so removing only one is not enough. All three are
parameterised in the unit; disable them from `.env`:

```bash
SYSTEMD_NO_NEW_PRIVILEGES=false
SYSTEMD_PRIVATE_DEVICES=false
SYSTEMD_PROTECT_KERNEL_MODULES=false
```

Then `systemctl --user daemon-reload && systemctl --user restart laya-service`.

Confirm what actually applied — do not assume:

```bash
systemctl --user show laya-service \
  -p NoNewPrivileges -p PrivateDevices -p ProtectKernelModules
```

On a normal host (not containerised) leave all three enabled.

> **Note on `ReadWritePaths` and the space in this path.** systemd's
> `ReadWritePaths` accepts neither quoting nor a bare backslash — a space splits
> the value into separate invalid paths that systemd silently drops with a
> warning. `%20` is *also* wrong: systemd reads `%` as a specifier and rejects
> the unit with `Invalid slot`. The working form is the C-style `\x20` escape,
> which is what the shipped unit uses.

---

## Observability

### Structured JSON logs

Every line is one JSON object:

```json
{"timestamp":"2026-09-22T10:14:03+0000","level":"INFO","logger":"laya_service.interfaces.http.middleware.access_log","message":"request completed","method":"POST","path":"/v1/predict","status":200,"latency_ms":1843.21,"request_id":"9f2c1e4b...","trace_id":"9f2c1e4b...","client":"127.0.0.1"}
```

Key fields: `request_id`, `trace_id`, `latency_ms`, `method`, `path`, `status`,
`client`.

Requests over 1 s are logged at `WARNING` so they surface without a separate
latency dashboard.

**Never logged:** request bodies, response bodies, the `Authorization` header.
A `SecretRedactingFilter` scrubs bearer tokens and `key=value` credential
patterns as a backstop.

### Correlation

`X-Request-ID` is honoured if the caller supplies a sane one (matching
`[A-Za-z0-9._:-]{1,128}`), otherwise generated. It is returned in the response
header and echoed in `meta.request_id`. The sanitisation blocks CRLF header
injection and log forging.

### Shipping to ELK / Loki

The output is newline-delimited JSON with no multi-line records (stack traces
are escaped into a single `exception` string field), so it can be consumed
without a custom parser:

```yaml
# Promtail example
scrape_configs:
  - job_name: laya-service
    static_configs:
      - targets: [localhost]
        labels:
          job: laya-service
          __path__: /path/to/laya-service/logs/laya.log
    pipeline_stages:
      - json:
          expressions:
            level: level
            request_id: request_id
            latency_ms: latency_ms
```

---

## Troubleshooting

### `configuration error: HOST=... but LAYA_API_KEY is empty`

Working as intended. Set `LAYA_API_KEY` in `.env`, or bind `HOST=127.0.0.1`.

### `/readyz` returns 503 forever

The model has not loaded. Causes, in order of likelihood:

1. **The weights are still downloading.** Watch the log — the adapter logs
   `loading laya model` before the download and `laya model loaded` after.
   First load can take several minutes.
2. **No disk space.** Weights need a few GB. `df -h`.
3. **`HF_ENDPOINT` unreachable.** Test with
   `curl -I https://hf-mirror.com`. Try `https://huggingface.co`.
4. **`laya` failed to import.** Check `.venv/bin/python -c "import laya"`.

The load error is cached, so a broken install fails fast on every subsequent
request rather than retrying a multi-minute download. Fix the cause and restart.

### `/v1/predict` returns 503 `model_load_failed`

Same as above. The log has the underlying exception; the client gets only the
correlation id.

### `CAS Client Error: HTTP status client error (401 Unauthorized), domain: https://cas-server.xethub.hf.co/...`

This one is specific and worth recognising. It means the **Xet** transfer
protocol — used by `huggingface_hub` via `hf_xet` for large files — is being
proxied by a mirror that does not support it. The mirror will happily serve
small files and its metadata API returns 200, which makes this confusing: the
failure is only in the CAS blob path.

Fix: disable Xet so `huggingface_hub` falls back to plain HTTP range requests.

```bash
echo 'HF_HUB_DISABLE_XET=1' >> .env
make restart
```

This is set by default in this deployment's `.env` for that reason. If you point
`HF_ENDPOINT` at the origin (`https://huggingface.co`) and have direct access,
you can remove it and get Xet's faster transfers back.

### `huggingface.co` is unreachable / requests hang

Some networks cannot reach the origin at all. `HF_ENDPOINT=https://hf-mirror.com`
is the default for this reason. Note that the mirror can lag upstream on *new*
releases, and (as above) does not proxy Xet.

### Requests take 60–90 seconds even when warm

On CPU, Laya inference over a handful of questions costs tens of seconds — the
`latency_ms` in a warm response reflects that (roughly 300–900 ms for a single
question on this host once the model is resident; much longer on the first call
while weights are paged in). Budget accordingly, and prefer the
`/v1/robot-dog/...` endpoint, which asks two questions in one forward pass
rather than two round trips.

### The first request takes minutes and the client times out

Expected on a cold cache. Either raise the client timeout (`timeout=600`), or
set `PRELOAD_MODEL=true` and accept a slow startup instead.

### `ModuleNotFoundError: No module named 'laya'`

```bash
make install     # or: .venv/bin/pip install -e .
```

Note that the Aliyun PyPI mirror lags upstream and may not have the newest
`laya`. The Makefile uses both indexes for this reason.

### `ImportError: libgomp.so.1: cannot open shared object file`

PyTorch needs OpenMP. On Debian/Ubuntu:

```bash
sudo apt-get install -y libgomp1
```

### `pip install torch` downloads 2.5 GB

You skipped the CPU-only step. See
[Why CPU-only PyTorch matters](#why-cpu-only-pytorch-matters).

### `make status` reports a CONFLICT between systemd and a script process

Both startup methods are alive but only one can hold the port; the other is
failing to bind and, under systemd, being restarted in a loop. Pick one:

```bash
make stop                          # kills the script-managed process
systemctl --user stop laya-service # or stops the systemd one
```

`make status` shows `restarts by systemd` — a non-zero count alongside a
CONFLICT line is the signature of exactly this. Check which one actually owns
the port under `== port ==`.

### `make start` succeeds but `make status` says "nothing listening"

The process bound a different interface or port than you expect. Check the
`starting uvicorn` line in `logs/laya.log` for the resolved `host`/`port`, and
confirm `.env` has no stray `PORT` entry.

### `Address already in use`

```bash
make status                       # is another instance running?
ss -ltnp | grep 9800              # who owns the port?
```

### Tests fail with `ImportError: No module named 'tests'`

Run pytest from the project root (`make test` does). The `tests` package is
resolved relative to the working directory.

---

## Limitations you must not ignore

**Laya emits zero-shot probabilities. They are not calibrated.**

This is the single most important caveat in this document. The `reliability`
value returned by `/v1/robot-dog/localization-reliability` is a *model-reported
confidence*, not a frequency. A value of `0.9` does **not** mean the judgement
is correct 90% of the time. Zero-shot confidence scores are typically
overconfident, and their miscalibration is not uniform across inputs.

Concretely, do not:

- Threshold `reliability` at a tuned value and treat the result as a
  probabilistic guarantee.
- Feed it into a downstream Bayesian filter as a likelihood without calibration.
- Report it to an operator as a percentage probability of correctness.
- Use it for safety-critical arbitration on its own.

Before any of that, **fine-tune on your own labelled trajectories and calibrate**
(temperature scaling, isotonic regression, or Platt scaling) against a held-out
set, then re-measure the reliability diagram and expected calibration error.

Until then, use the output as a *ranking signal* and a *trigger for a
conservative fallback*. The service's own policy follows this: when the model is
not confident, it recommends `switch_to_visual_imu` regardless of the verdict.

**Other limitations:**

- **Cold start is slow.** Minutes on a cold cache; plan for it in probes and
  timeouts.
- **Single-process by default.** The model is several GB of RSS. On a 14 GB host,
  more than one Uvicorn worker will likely OOM. Measure before scaling out;
  scale *horizontally* across machines rather than adding workers.
- **CPU inference is slow.** Budget seconds per request, not milliseconds.
- **No request queue or admission control.** Concurrent requests all load the
  model and then contend for CPU. Put a bounded concurrency limit in front of
  this service if you expect load.
- **The `MAX_BODY_BYTES` setting is validated but not enforced by a body-size
  middleware.** Uvicorn does not enforce it either. If you need a hard cap,
  enforce it at the reverse proxy.

---

## Security

### API key

- Generate with `secrets.token_urlsafe(32)` — never a human-chosen string.
- The key is compared with `hmac.compare_digest` (constant time), so response
  latency does not leak it byte by byte.
- Auth failures are uniform: the client learns *that* authentication failed and
  nothing else.
- The key is redacted from startup logs and scrubbed from any log line by the
  redaction filter.
- **Rotate it** by editing `.env` and running `make restart`. There is no
  dual-key overlap, so rotation is briefly disruptive — do it during a
  maintenance window, or put a proxy in front if you need zero-downtime
  rotation.

### Network exposure

- **Never bind `0.0.0.0` without a key.** The settings validator blocks this, but
  the same care applies to whatever you put in front.
- Restrict the security group / firewall to the specific source ranges that need
  access. Port 9800 should not be open to `0.0.0.0/0`.
- Prefer a reverse proxy (nginx, Caddy, an ALB) terminating TLS. **This service
  speaks plain HTTP.** A bearer token over plain HTTP is a token anyone on the
  path can read.
- If you expose it publicly, add rate limiting at the proxy. There is none here,
  and each request costs real CPU.

### CORS

`CORS_ORIGINS` is empty by default, which **disables CORS entirely** — correct
for a server-to-server API. Only set it if a browser must call this service
directly, and list exact origins. Never use `*`: combined with a bearer token it
invites credential leakage through a compromised page.

### Body size

`MAX_BODY_BYTES` defaults to 64 KiB. See the caveat in
[Limitations](#limitations-you-must-not-ignore) — this is currently a validated
setting rather than an enforced limit. Enforce it at the proxy if it matters.

### Container and unit hardening

The Docker image runs as a non-root user with no write access to application
code. The systemd unit sets `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`,
`ProtectHome=read-only`, and re-opens only the three paths the service genuinely
needs to write.

---

## License

MIT
