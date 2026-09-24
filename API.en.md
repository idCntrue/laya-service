# Laya Service — API Reference

HTTP interface for the Laya decision-model inference service.

| | |
|---|---|
| Base URL (local) | `http://127.0.0.1:9800` |
| Base URL (LAN) | `http://<LAN-IP>:9800` |
| Base URL (public) | `http://<PUBLIC-IP>:9800` — **the security group must allow 9800** |
| Interactive docs | `http://<host>:9800/docs` (Swagger UI) |
| OpenAPI schema | `http://<host>:9800/openapi.json` |
| Authentication | `Authorization: Bearer <LAYA_API_KEY>` |
| Content type | `application/json` |
| Version | `0.1.0` |

> **中文版：** [API.md](API.md). Both language versions are kept in sync; if they
> disagree, the English one describes the code and the Chinese one is a bug.

---

## Contents

- [Quick start](#quick-start)
- [Authentication](#authentication)
- [Response envelope](#response-envelope)
- [Error codes](#error-codes)
- [Endpoints](#endpoints)
  - [GET /healthz — liveness](#get-healthz--liveness)
  - [GET /readyz — readiness](#get-readyz--readiness)
  - [POST /v1/predict — generic inference](#post-v1predict--generic-inference)
  - [POST /v1/robot-dog/localization-reliability](#post-v1robot-doglocalization-reliability)
- [Calling the service](#calling-the-service)
- [The one concept people get wrong: `noul` is not a boolean](#the-one-concept-people-get-wrong-noul-is-not-a-boolean)
- [Timeouts and performance](#timeouts-and-performance)
- [Compatibility layer: OpenAI / Anthropic](#compatibility-layer-openai--anthropic)
- [API key administration](#api-key-administration)
- [Troubleshooting](#troubleshooting)

---

## Quick start

```bash
# 1. Get the API key (run this on the server)
cd /path/to/laya-service
export LAYA_API_KEY="$(grep '^LAYA_API_KEY=' .env | cut -d= -f2-)"

# 2. Confirm the service is alive
curl http://127.0.0.1:9800/healthz

# 3. Run an inference
curl -X POST http://127.0.0.1:9800/v1/predict \
  -H "Authorization: Bearer $LAYA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"state":{"observation":"The x coordinate jumped 2.3 meters."},
       "questions":{"q":{"type":"noul","instructions":"Is the localization reliable?"}}}'
```

---

## Authentication

Every `/v1/*` route requires a bearer token:

```
Authorization: Bearer <LAYA_API_KEY>
```

**No authentication required:** `/healthz`, `/readyz`, `/docs`, `/redoc`,
`/openapi.json`.

Authentication failures always return **401**, and deliberately do **not**
distinguish "no token supplied" from "wrong token" — that would leak information.
Comparison uses a constant-time algorithm to prevent timing side channels.

> **Security note.** This service speaks plain HTTP, so the key is readable by
> anyone on the network path. Terminate TLS at a reverse proxy, or restrict
> access by source IP at the firewall, before exposing it publicly.

---

## Response envelope

**Success:**

```json
{
  "ok": true,
  "data": { ... },
  "meta": {
    "backend": "auto",
    "model": "convaiinnovations/laya",
    "request_id": "b8434ab7ef0d47cd91b92eaf2a084d6c",
    "latency_ms": 388.28
  }
}
```

**Failure** — the same shape for every error path:

```json
{
  "ok": false,
  "error": {
    "code": "unauthorized",
    "message": "a valid bearer token is required"
  },
  "request_id": "e4b4df6b9ea54428b35c231a1954eb7a"
}
```

`meta` fields:

| Field | Type | Description |
|---|---|---|
| `backend` | string | Compute backend, e.g. `auto` / `cpu` / `cuda` |
| `model` | string | The model identifier that actually served the request |
| `request_id` | string | Correlation id — quote this when reporting a problem |
| `latency_ms` | float | Server-side handling time, in milliseconds |

### Correlation IDs

Responses carry `X-Request-ID` and `X-Trace-ID` headers.

- A client **may** supply its own `X-Request-ID` for cross-system tracing. The
  service passes it through and echoes it in `meta.request_id`.
- An invalid id (illegal characters) is replaced with a freshly generated one,
  which blocks CRLF header injection and log forging.
- When reporting a problem, include the `request_id` — it pinpoints the exact
  log line.

---

## Error codes

| `error.code` | HTTP | Meaning | What to do |
|---|---|---|---|
| `unauthorized` | 401 | Token missing, malformed, or wrong | Check the `Authorization` header |
| `forbidden` | 403 | Not permitted | Check credentials |
| `not_found` | 404 | No such path | Check the URL |
| `method_not_allowed` | 405 | Wrong HTTP verb | Use POST |
| `validation_error` | 422 | Body failed schema validation | The `message` names the offending field |
| `invalid_probability` | 400 | A probability fell outside `[0, 1]` | Fix the input |
| `invalid_localization_summary` | 400 | Summary is internally incoherent | Fix the input |
| `invalid_question` | 400 | Question definition is malformed | Check `type` / `instructions` / `criteria` |
| `invalid_decision` | 400 | Decision carried unusable data | Fix the input |
| `unsupported_schema` | 400 | A tool schema has no mapping to a question the model can answer | See the compatibility layer |
| `unsupported_model` | 400 | A model was requested that this service does not serve | Query `GET /v1/models` |
| `invalid_api_key` | 400 | An API-key request carried unusable fields | Check name / scopes / expires_at |
| `model_load_failed` | 503 | Model could not be loaded (missing dep, download failure, no disk) | **Retryable** — check server logs |
| `model_inference_failed` | 503 | Model raised during inference | **Retryable** |
| `internal_error` | 500 | Unhandled exception | Report with the `request_id` |
| `http_error` | other | Any other HTTP error raised by the framework | Inspect the status code |
| `payload_too_large` | 413 | ⚠️ **Currently unreachable** — see below | Limit at the reverse proxy |

**503 is retryable** — use exponential backoff. 500 should not be retried
blindly.

> ⚠️ **`MAX_BODY_BYTES` is validated but not enforced.**
>
> The setting is range-checked at startup, but **no middleware actually rejects
> an oversized body**, and Uvicorn does not limit it either. So the
> `413 payload_too_large` path is **currently unreachable** — a 200 KB body was
> measured returning 200.
>
> For a hard limit, enforce it at the **reverse proxy** (nginx
> `client_max_body_size`). See
> [SECURITY.md](SECURITY.md#known-limitations).

> **Authentication runs before routing.** An unauthenticated request to a
> non-existent path returns 401, not 404. This is deliberate: it stops an
> unauthenticated caller from enumerating which routes exist by diffing 404
> against 401.

---

## Endpoints

### GET /healthz — liveness

**Auth:** not required.

Answers *is the process alive?* Always returns 200. Never touches the model,
never touches the network. A failure here means the process is wedged and the
orchestrator should restart it.

```bash
curl http://127.0.0.1:9800/healthz
```

**200**
```json
{"status": "ok", "version": "0.1.0"}
```

---

### GET /readyz — readiness

**Auth:** not required.

Answers *should this replica receive traffic?* Returns **503** until the model is
loaded and warm.

```bash
curl http://127.0.0.1:9800/readyz
```

**200 — ready**
```json
{
  "status": "ready",
  "model_loaded": true,
  "backend": "auto",
  "model": "convaiinnovations/laya",
  "detail": null
}
```

**503 — not ready**
```json
{
  "status": "not_ready",
  "model_loaded": false,
  "backend": "auto",
  "model": "convaiinnovations/laya",
  "detail": "the decision model has not been loaded yet; it loads on first request. Send a request to /v1/predict to trigger the load, or set PRELOAD_MODEL=true."
}
```

> ⚠️ **Never wire a liveness probe to `/readyz`.** A cold start downloads several
> GB of weights and takes minutes. A container whose liveness probe is
> `/readyz` would be restarted mid-download, repeatedly, and never become
> ready. Use `/healthz` for liveness and `/readyz` for load-balancer membership.

---

### POST /v1/predict — generic inference

Ask arbitrary questions about an arbitrary state.

**Auth:** required.

**Request body**

| Field | Type | Required | Description |
|---|---|---|---|
| `state` | object | yes | Situation description, JSON-serialisable. `{"observation": "<english text>"}` is the recommended form |
| `questions` | object | yes | Questions keyed by id. At least one |

**Question definition**

| Field | Type | Required | Description |
|---|---|---|---|
| `type` | string | yes | `noul` \| `choice` \| `score` |
| `instructions` | string | yes | English question text, non-empty |
| `criteria` | object/array | conditional | Mapping for `choice`, list for `score`. Not used by `noul` |

| `type` | Answer shape | Needs `criteria` |
|---|---|---|
| `noul` | float — **probability that the answer is true** | no |
| `choice` | string — the winning option key | yes (mapping `{key: description}`) |
| `score` | float — expected value of the ordinal rating | yes (list, lowest first) |

> **English only.** Laya is trained on English; other languages degrade accuracy
> unpredictably.

**Request**

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

**200**

```json
{
  "ok": true,
  "data": {
    "answers": {
      "location_reliable": {
        "type": "noul",
        "noul": 0.2281,
        "confidence": 0.7719,
        "action": {"act_probability": 1.0}
      }
    }
  },
  "meta": {
    "backend": "auto",
    "model": "convaiinnovations/laya",
    "request_id": "6748e31d8abe4100a7fd75007ecba203",
    "latency_ms": 388.28
  }
}
```

**Answer fields** — returned by Laya unchanged; the service does not rewrite them.

Fields common to every type:

| Field | Description |
|---|---|
| `type` | Question type, echoed back |
| `confidence` | The model's confidence **in this answer**, `[0,1]` |
| `action` | Laya's auxiliary signal (`act_probability`); usually ignorable |

Type-specific fields:

| `type` | Field | Type | Description |
|---|---|---|---|
| `noul` | `noul` | float | Probability that the answer is true |
| `choice` | `choice` | string | The winning option key |
| `choice` | `probabilities` | object | Full distribution over options |
| `score` | `score` | float | Expected value of the ordinal rating |
| `score` | `legend` | object | Index → `criteria` description |
| `score` | `probabilities` | object | Full distribution over levels |

**A real `score` / `choice` response** (measured):

```json
{
  "reliable": {"type": "noul", "noul": 0.2369, "confidence": 0.7631,
               "action": {"act_probability": 1.0}},
  "severity": {"type": "score", "score": 1.2513,
               "legend": {"0": "negligible", "1": "minor", "2": "moderate", "3": "severe"},
               "probabilities": {"0": 0.1257, "1": 0.5439, "2": 0.2838, "3": 0.0466},
               "confidence": 0.2121, "action": {"act_probability": 1.0}},
  "cause": {"type": "choice", "choice": "gps",
            "probabilities": {"gps": 0.8965, "slip": 0.05, "imu": 0.0536},
            "confidence": 0.6318, "action": {"act_probability": 1.0}}
}
```

For `score`, `legend` maps the index back to the `criteria` description so you
can read it directly; a `score` of `1.2513` means the expectation sits between
`minor` and `moderate`. `probabilities` carries the full distribution, which is
far more informative than the point value — prefer it for downstream decisions.

**Multi-question request**

```json
{
  "state": {"observation": "The robot is indoors with weak GPS."},
  "questions": {
    "reliable": {"type": "noul", "instructions": "Is localization reliable?"},
    "severity": {"type": "score", "instructions": "How severe is the drift?",
                 "criteria": ["negligible", "minor", "moderate", "severe"]},
    "cause": {"type": "choice", "instructions": "What is the likely cause?",
              "criteria": {"gps": "GPS multipath", "slip": "Wheel slip", "imu": "IMU drift"}}
  }
}
```

**Errors**

`422` — missing field, unsupported type, or `choice`/`score` without `criteria`:

```json
{"ok": false, "error": {"code": "validation_error",
 "message": "questions: Value error, question 'q' of type 'choice' requires a 'criteria' field (a list for 'score', a mapping for 'choice')"}}
```

---

### POST /v1/robot-dog/localization-reliability

Robot-dog endpoint. Turns a structured summary of recent localization behaviour
into a reliability verdict plus a recommended action.

**Auth:** required.

> **How this differs from `/v1/predict`:** this endpoint bakes in **business
> policy** — the decision threshold, the fallback rule, and the recommendation
> mapping — and returns something directly usable for a control decision. Use
> `/v1/predict` if you want the model's raw output.

**Request body**

| Field | Type | Range | Required | Description |
|---|---|---|---|---|
| `x_jump_m` | float | `0 … 1000` | yes | Discontinuous jump along x, in metres |
| `y_stable` | bool | — | yes | Whether y stayed stable |
| `heading_reversals` | int | `0 … 1000` | yes | Heading reversals in the window |
| `confidence_start` | float | `0 … 1` | yes | Estimator confidence at window start |
| `confidence_end` | float | `0 … 1` | yes | Estimator confidence at window end |
| `environment` | string | 1–128 chars | yes | Environment tag |
| `confidence_threshold` | float | `0 … 1`, default `0.5` | no | Bar for `confident` |

**Known `environment` values:** `indoor_weak_gps`, `indoor_strong_gps`,
`outdoor_open_sky`, `outdoor_urban_canyon`, `underground`, `indoor`, `outdoor`.
Unknown tags are accepted and rendered into readable English.

**Request**

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

**200**

```json
{
  "ok": true,
  "data": {
    "reliable": false,
    "reliability": 0.8382,
    "recommendation": "switch_to_visual_imu",
    "confident": true
  },
  "meta": {
    "backend": "auto",
    "model": "convaiinnovations/laya",
    "request_id": "a5f094cb3c274e88b0b8eaf376a711d1",
    "latency_ms": 726.28
  }
}
```

**Response fields**

| Field | Type | Description |
|---|---|---|
| `reliable` | bool | Whether localization is judged trustworthy, derived by comparing the `noul` probability against **0.5** |
| `reliability` | float | The model's **confidence in that judgement**, `[0,1]` |
| `recommendation` | string | Suggested action — see below |
| `confident` | bool | Whether `reliability` cleared `confidence_threshold` |

**`recommendation` values**

| Value | Meaning |
|---|---|
| `continue_with_current_estimator` | Keep using the current estimator |
| `switch_to_visual_imu` | Switch to visual-inertial odometry |

**Decision logic** (business policy, in the application layer — not the route):

1. If `confident == false` → `switch_to_visual_imu`
   **When the model is unsure, the service always recommends the conservative
   action.** An unnecessary fallback to visual-inertial odometry costs
   efficiency; trusting a bad pose estimate can collide with something.
2. Else if `reliable == false` → `switch_to_visual_imu`
3. Else → `continue_with_current_estimator`

> **Important:** `reliable` and `reliability` are **two different quantities**.
> The first is the model's **answer** (the `noul` probability); the second is the
> model's **confidence in that answer**. A client should check `confident`
> first:

```python
if not data["confident"]:
    fallback()                    # model is unsure — do NOT trust `reliable`
elif data["reliable"]:
    trust_the_estimate()
else:
    switch_to_visual_imu()
```

**Errors**

`422` — value out of range:

```json
{"ok": false, "error": {"code": "validation_error",
 "message": "x_jump_m: Input should be greater than or equal to 0"}}
```

---

## Calling the service

### curl (with variables)

```bash
export BASE=http://127.0.0.1:9800
export LAYA_API_KEY="$(grep '^LAYA_API_KEY=' .env | cut -d= -f2-)"

curl -sS -X POST "$BASE/v1/robot-dog/localization-reliability" \
  -H "Authorization: Bearer $LAYA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"x_jump_m":2.3,"y_stable":true,"heading_reversals":3,
       "confidence_start":0.9,"confidence_end":0.4,"environment":"indoor_weak_gps"}' \
  | python3 -m json.tool
```

### Python (httpx)

```python
import os
import httpx

BASE = "http://127.0.0.1:9800"
HEADERS = {"Authorization": f"Bearer {os.environ['LAYA_API_KEY']}"}

# timeout must be generous: a cold start downloads weights and can take minutes
with httpx.Client(base_url=BASE, headers=HEADERS, timeout=600.0) as client:
    r = client.post("/v1/robot-dog/localization-reliability", json={
        "x_jump_m": 2.3,
        "y_stable": True,
        "heading_reversals": 3,
        "confidence_start": 0.9,
        "confidence_end": 0.4,
        "environment": "indoor_weak_gps",
    })
    r.raise_for_status()
    body = r.json()

data = body["data"]
request_id = body["meta"]["request_id"]   # quote this when reporting a problem

if not data["confident"]:
    fallback()                  # model is unsure — fall back conservatively
elif data["reliable"]:
    trust_the_estimate()
else:
    switch_to_visual_imu()
```

### Python (requests)

```python
import os, requests

r = requests.post(
    "http://127.0.0.1:9800/v1/predict",
    headers={"Authorization": f"Bearer {os.environ['LAYA_API_KEY']}"},
    json={
        "state": {"observation": "The x coordinate jumped 2.3 meters."},
        "questions": {"q": {"type": "noul",
                            "instructions": "Is the localization reliable?"}},
    },
    timeout=600,
)
r.raise_for_status()
print(r.json()["data"]["answers"]["q"]["noul"])
```

### Error-handling template

```python
import httpx, time

def call_with_retry(url, payload, headers, attempts=3):
    """503 is retryable; 4xx is not."""
    for i in range(attempts):
        r = httpx.post(url, json=payload, headers=headers, timeout=600)
        if r.status_code == 200:
            return r.json()["data"]
        if r.status_code in (401, 403, 404, 422):
            raise ValueError(f"client error, retrying will not help: {r.json()['error']['message']}")
        if r.status_code == 503 and i < attempts - 1:
            time.sleep(2 ** i)          # exponential backoff
            continue
        raise RuntimeError(f"server error {r.status_code}: {r.json()}")
    raise RuntimeError("retries exhausted")
```

---

## The one concept people get wrong: `noul` is not a boolean

This is the single most likely way to misuse the service.

A `noul` answer is a **float — the probability that the answer is true** — not
`true`/`false`:

```json
{"type": "noul", "noul": 0.2281, "confidence": 0.7719}
```

| Field | Meaning |
|---|---|
| `noul` = 0.2281 | The probability that "reliable" is true is **22.8%** → i.e. **unreliable** |
| `confidence` = 0.7719 | The model is **77% confident** in that 22.8% judgement |

**A model can be 95% confident that the answer is false:**
`{"noul": 0.05, "confidence": 0.95}`.

The two must be used separately:

- "What is the answer?" → use `noul` (threshold at 0.5)
- "Can I trust that answer?" → use `confidence` (compare against your bar)

> ⚠️ **Zero-shot probabilities are not calibrated.** `confidence = 0.77` does
> **not** mean "correct 77% of the time". Zero-shot confidence is typically
> overconfident, and miscalibration is not uniform across inputs. Measured on
> this service, incorrect answers carried confidence of 0.81, 0.85 and 0.92 —
> **it is confidently wrong sometimes**, so you cannot filter errors by
> thresholding `confidence` alone.
>
> **Do not:** threshold it and treat the result as a probabilistic guarantee;
> feed it into a Bayesian filter as a likelihood; show it to an operator as a
> success rate; use it alone for safety-critical arbitration.
>
> **Do:** treat it as a ranking signal and a trigger for conservative fallback,
> and **measure accuracy on your own labelled data** before relying on it.

### What the model is good at

Measured against this service:

| Task type | Result |
|---|---|
| Semantic classification (sentiment, spam, intent, content safety) | 8/8 correct |
| Reasoning (arithmetic, temporal, multi-step) | 3/6 correct |

It reads text; it does not compute. It **cannot** do arithmetic, spatial
reasoning, planning, or search — for those, a deterministic algorithm wins on
both accuracy and cost. See [README](README.md#limitations-you-must-not-ignore).

---

## Timeouts and performance

| Scenario | Measured | Recommended client timeout |
|---|---|---|
| Cold-start first request (downloads several GB of weights) | ~90 s | **≥ 600 s** |
| Warm single-question inference | 300–900 ms | 30 s is plenty |
| Warm robot-dog endpoint (2 questions, one forward pass) | 500–800 ms | 30 s is plenty |

**Give cold starts enough time**, or the first request will always time out.

### Other limits

- **Single process.** The model holds ~2 GB RSS. Do **not** add multiple workers
  on a 14 GB host — it will OOM. Scale horizontally instead.
- **No queue and no admission control.** Concurrent requests all load the model
  and then contend for CPU. Add a concurrency limit at the proxy if you expect
  load.
- **CPU inference is slow.** Budget in seconds, not milliseconds.

---

## Compatibility layer: OpenAI / Anthropic

This service exposes **OpenAI-** and **Anthropic-compatible** endpoints, so an
unmodified official SDK can connect to it. Point the SDK's `base_url` here; no
client code changes.

> ### ⚠️ Tool calling only — not chat
>
> **The model behind this service is a classifier. It does not generate text.**
> It reads English and answers structured questions; it cannot write a sentence,
> and that is fixed by the model architecture.
>
> So the compatibility layer maps the **tool calling / tool use** capability of
> both APIs: you describe *what to decide* with a tool schema, and the answer
> arrives as the tool call's arguments.
>
> **A chat request is rejected (400) rather than answered with something that
> looks like a reply.** That is deliberate: a faked generation is
> indistinguishable from a real one, and a caller would build on it.

### Mapping rules

Each property of the tool schema becomes one model question:

| JSON Schema | Question type | Notes |
|---|---|---|
| `enum` (strings) | `choice` | **Order is the label order**; it is never sorted |
| `oneOf` + `const` | `choice` | The standard form; `description` becomes the rubric |
| `enum` + `enumDescriptions` | `choice` | OpenAI's convention for per-option rubrics |
| `boolean` | `noul` | ⚠️ **Lossy** — an explicit threshold is required |
| `number` (with min/max) | `score` | Returns the expected value, scaled to your range |
| `integer` (with min/max) | `score` | Rounded to an integer to honour your schema |

**Shapes with no mapping are rejected (400 `unsupported_schema`)**: free-form
strings, arrays, and nested objects. Guessing an answer is more dangerous than
failing, for the same reason as above.

#### Why `boolean` requires a threshold

`noul` returns the **probability that the answer is true**, not a decision. Any
threshold we applied would be invented on your behalf, and the probability would
be destroyed — you could not re-threshold it. So:

```jsonc
{
  "type": "boolean",
  "description": "Is this spam?",
  "x-laya-threshold": 0.7      // required
}
```

To receive the probability itself, use a numeric property:

```jsonc
{"type": "number", "minimum": 0, "maximum": 1, "description": "Spam likelihood"}
```

### base_url

| SDK | base_url | Auth header |
|---|---|---|
| OpenAI | `http://<host>:9800/v1` | `Authorization: Bearer <key>` |
| Anthropic | `http://<host>:9800` | `x-api-key: <key>` |

The server accepts both header shapes, so a bearer token also works against the
Anthropic endpoint.

### Anthropic example

```python
from anthropic import Anthropic

client = Anthropic(
    base_url="http://127.0.0.1:9800",      # note: no /v1
    api_key="laya_sk_...",                 # or the LAYA_API_KEY from .env
)

message = client.messages.create(
    model="english",
    max_tokens=1024,                       # required by the SDK; ignored here
    messages=[{
        "role": "user",
        "content": "Order #12345 arrived broken and I want my money back",
    }],
    tools=[{
        "name": "classify_ticket",
        "description": "Classify a support ticket.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["refund", "technical", "billing"],
                    "description": "Which team should handle this?",
                },
                "churn_risk": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "How likely is the customer to leave?",
                },
                "needs_human": {
                    "type": "boolean",
                    "description": "Does this need a human agent?",
                    "x-laya-threshold": 0.5,
                },
            },
        },
    }],
    tool_choice={"type": "tool", "name": "classify_ticket"},
)

# The answer is the tool_use block's input -- an object, not a JSON string
block = message.content[0]
print(block.type)        # "tool_use"
print(block.name)        # "classify_ticket"
print(block.input)       # {'category': 'refund', 'churn_risk': 0.71, 'needs_human': True}
print(message.stop_reason)   # "tool_use"
```

### OpenAI example

`POST /v1/chat/completions` — the OpenAI counterpart of `POST /v1/messages`.

```python
import json
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:9800/v1",   # note: includes /v1
    api_key="laya_sk_...",
)

response = client.chat.completions.create(
    model="english",
    messages=[{"role": "user", "content": "The app crashes when I upload a photo"}],
    tools=[{
        "type": "function",
        "function": {
            "name": "classify_ticket",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["refund", "technical", "billing"],
                        "description": "Which team should handle this?",
                    },
                },
            },
        },
    }],
    tool_choice={"type": "function", "function": {"name": "classify_ticket"}},
)

call = response.choices[0].message.tool_calls[0]
print(call.function.name)                        # "classify_ticket"
print(json.loads(call.function.arguments))       # {'category': 'technical'}
print(response.choices[0].finish_reason)         # "tool_calls"
```

> Note the difference: Anthropic's `input` is an **object**; OpenAI's
> `arguments` is a **JSON string** that the SDK parses for you.

### curl example (Anthropic format)

```bash
curl -X POST http://127.0.0.1:9800/v1/messages \
  -H "x-api-key: $LAYA_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "english",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "I was charged twice this month"}],
    "tools": [{
      "name": "classify",
      "input_schema": {
        "type": "object",
        "properties": {
          "category": {
            "type": "string",
            "enum": ["billing", "technical", "refund"],
            "description": "Which team should handle this?"
          }
        }
      }
    }],
    "tool_choice": {"type": "tool", "name": "classify"}
  }'
```

### GET /v1/models

Lists the accepted `model` values.

```bash
curl http://127.0.0.1:9800/v1/models -H "Authorization: Bearer $LAYA_API_KEY"
```

```json
{"object": "list", "data": [{"id": "english", "object": "model",
  "created": 0, "owned_by": "laya-service"}]}
```

### Ignored fields

These are accepted and have **no effect** — the service generates nothing, so
they are meaningless:

`temperature`, `top_p`, `top_k`, `max_tokens`, `stop`, `seed`,
`presence_penalty`, `frequency_penalty`, `logprobs`, `response_format`, `user`,
`metadata`, `system`

**Rejected fields** (silently ignoring these would produce a response the caller
misreads, so they error):

| Field | Reason |
|---|---|
| `stream: true` | Every answer comes from one forward pass; there is nothing to stream |
| `n > 1` | One forward pass yields one answer set; returning fewer would be silent |

### The `_laya` block

Both compat responses carry a `_laya` metadata block (non-standard; ignoring it
is fine):

```json
"_laya": {
  "backend": "auto",
  "latency_ms": 412.3,
  "derived_thresholds": {"needs_human": 0.5},
  "note": "Probabilities from this model are not calibrated. ..."
}
```

`derived_thresholds` records which boolean properties were **derived** and at
what threshold, so "the probability the model produced" and "the decision we
made for you" stay distinguishable.

---

## API key administration

Beyond the `LAYA_API_KEY` in `.env`, additional keys can be managed through
`/admin/keys`.

> **The `.env` key is the administrator key.** It always works, it is what you
> use to create the first key, and it is the recovery path if every stored key is
> revoked. It is never written to the key file.

Every `/admin/*` route requires the **admin** scope, so an inference-only key
cannot issue itself a replacement.

### Storage

Keys live in a JSON file (default `data/api_keys.json`, override with
`API_KEYS_PATH`):

- **Only `sha256` is stored, never the plaintext.** The plaintext appears once,
  in the create response, and is unrecoverable afterwards.
- The file is created `0600`.
- Comparison is constant-time and **walks every record without short-circuiting**,
  so response latency reveals neither whether a key exists nor which one matched.

### `GET /admin/keys`

```bash
curl "http://127.0.0.1:9800/admin/keys?include_revoked=false" \
  -H "Authorization: Bearer $LAYA_API_KEY"
```

```json
{
  "keys": [{
    "id": "32e64954c0244a91bb51f642cfef16e4",
    "name": "grafana-prod",
    "prefix": "laya_sk_u825",
    "scopes": ["inference"],
    "created_at": "2026-09-24T03:42:48+00:00",
    "expires_at": null,
    "revoked_at": null,
    "last_used_at": null
  }],
  "available_scopes": ["admin", "inference"],
  "default_scopes": ["inference"]
}
```

### `POST /admin/keys`

```bash
curl -X POST http://127.0.0.1:9800/admin/keys \
  -H "Authorization: Bearer $LAYA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "grafana-prod", "scopes": ["inference"]}'
```

```json
{
  "id": "32e64954c0244a91bb51f642cfef16e4",
  "name": "grafana-prod",
  "prefix": "laya_sk_u825",
  "scopes": ["inference"],
  "created_at": "2026-09-24T03:42:48+00:00",
  "secret": "laya_sk_u825..."
}
```

> ⚠️ **`secret` appears in this one response only.** Store it; it cannot be
> retrieved again. Omitting `scopes` grants `inference` alone — issuing an
> administrator requires writing `["admin", "inference"]` explicitly.

### `GET /admin/keys/{key_id}`

```bash
curl http://127.0.0.1:9800/admin/keys/32e64954c0244a91bb51f642cfef16e4 \
  -H "Authorization: Bearer $LAYA_API_KEY"
```

### `PATCH /admin/keys/{key_id}`

Change the label, scopes, or expiry. **Fields you omit are left unchanged**:

```bash
curl -X PATCH http://127.0.0.1:9800/admin/keys/32e64954c0244a91bb51f642cfef16e4 \
  -H "Authorization: Bearer $LAYA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "grafana-staging"}'
```

### `DELETE /admin/keys/{key_id}`

Revoke. Takes effect **immediately**; the record is kept for auditing:

```bash
curl -X DELETE http://127.0.0.1:9800/admin/keys/32e64954c0244a91bb51f642cfef16e4 \
  -H "Authorization: Bearer $LAYA_API_KEY"
```

---

## Troubleshooting

**`/readyz` returns 503 forever.**
The model has not finished loading. A cold start downloads several GB. Look for
`laya model loaded` in the server log. To load at startup instead, set
`PRELOAD_MODEL=true`.

**The first request is very slow and the client times out.**
Expected on a cold cache. Raise the client timeout to 600 s, or set
`PRELOAD_MODEL=true` so startup pays the cost instead.

**503 `model_load_failed`.**
The model failed to load. Common causes: disk full, HF endpoint unreachable,
`laya` not installed. The response carries only a `request_id`; the actual
exception is in the server log. **Retryable.**

**Why does an unauthenticated request to a non-existent path return 401, not 404?**
Authentication runs before routing, deliberately, so an unauthenticated caller
cannot enumerate paths by diffing 404 against 401.

**How do I confirm the service is running?**
```bash
cd /path/to/laya-service && make status   # shows mode, process, port, probes
```

**A laptop on the public internet cannot connect.**
1. Confirm the service is up on the server: `make status`
2. Confirm the Alibaba Cloud **security group allows 9800** (inbound; restrict
   the source — do not use `0.0.0.0/0`)
3. Test from the laptop: `curl -m 5 http://<PUBLIC-IP>:9800/healthz`

**What is the relationship between `reliable` and `reliability`?**
`reliable` is the conclusion; `reliability` is the confidence in the conclusion.
Check `confident` (whether `reliability` cleared the threshold) first, then
`reliable`. See [the section above](#the-one-concept-people-get-wrong-noul-is-not-a-boolean).
