"""Integration tests over the full HTTP stack.

These exercise the real FastAPI application -- real middleware, real exception
handlers, real routing -- with only the decision-model dependency swapped for
the deterministic fake. That combination tests everything the architecture
actually wires together while staying fast enough to run on every commit.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from laya_service.domain.exceptions import ModelInferenceError, ModelLoadError
from tests.conftest import TEST_API_KEY, FakeDecisionModel

PREDICT_BODY = {
    "state": {
        "observation": (
            "Over the past 3 seconds, the x coordinate jumped discontinuously by "
            "about 2.3 meters, y was stable, heading reversed three times, and "
            "positioning confidence dropped from 0.9 to 0.4."
        )
    },
    "questions": {
        "location_reliable": {
            "type": "noul",
            "instructions": "Is the current localization reliable?",
        }
    },
}

DOG_BODY = {
    "x_jump_m": 2.3,
    "y_stable": True,
    "heading_reversals": 3,
    "confidence_start": 0.9,
    "confidence_end": 0.4,
    "environment": "indoor_weak_gps",
}


class TestHealthEndpoints:
    """Liveness and readiness."""

    def test_healthz_returns_200(self, anon_client: TestClient) -> None:
        """Liveness is unauthenticated and always 200."""
        response = anon_client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_healthz_reports_a_version(self, anon_client: TestClient) -> None:
        """The version is surfaced for operational visibility."""
        assert anon_client.get("/healthz").json()["version"]

    def test_readyz_returns_200_when_model_is_ready(self, anon_client: TestClient) -> None:
        """Readiness is 200 once the model reports itself loaded."""
        response = anon_client.get("/readyz")
        assert response.status_code == 200
        assert response.json()["model_loaded"] is True

    def test_readyz_returns_503_when_model_is_not_loaded(
        self, app: FastAPI, anon_client: TestClient
    ) -> None:
        """Readiness is 503 before the model is warm, and explains why."""
        from laya_service.interfaces.http.dependencies import get_decision_model

        app.dependency_overrides[get_decision_model] = lambda: FakeDecisionModel(ready=False)
        response = anon_client.get("/readyz")
        assert response.status_code == 503
        body = response.json()
        assert body["model_loaded"] is False
        assert body["detail"]

    def test_healthz_does_not_require_auth(self, anon_client: TestClient) -> None:
        """Probes must be reachable before credentials exist."""
        assert anon_client.get("/healthz").status_code == 200

    def test_readyz_does_not_require_auth(self, anon_client: TestClient) -> None:
        """Readiness is reachable without credentials."""
        assert anon_client.get("/readyz").status_code in (200, 503)


class TestAuthentication:
    """The auth middleware."""

    def test_predict_without_token_is_401(self, anon_client: TestClient) -> None:
        """An unauthenticated request is rejected."""
        response = anon_client.post("/v1/predict", json=PREDICT_BODY)
        assert response.status_code == 401

    def test_predict_with_wrong_token_is_401(self, anon_client: TestClient) -> None:
        """A wrong token is rejected identically to a missing one."""
        response = anon_client.post(
            "/v1/predict",
            json=PREDICT_BODY,
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert response.status_code == 401

    def test_malformed_authorization_header_is_401(self, anon_client: TestClient) -> None:
        """A header without the Bearer scheme is rejected."""
        response = anon_client.post(
            "/v1/predict", json=PREDICT_BODY, headers={"Authorization": TEST_API_KEY}
        )
        assert response.status_code == 401

    def test_401_uses_the_error_envelope(self, anon_client: TestClient) -> None:
        """Auth failures use the same envelope as every other error."""
        body = anon_client.post("/v1/predict", json=PREDICT_BODY).json()
        assert body["ok"] is False
        assert body["error"]["code"] == "unauthorized"

    def test_401_does_not_leak_the_expected_key(self, anon_client: TestClient) -> None:
        """The response never contains the configured secret."""
        text = anon_client.post("/v1/predict", json=PREDICT_BODY).text
        assert TEST_API_KEY not in text

    def test_401_sets_www_authenticate(self, anon_client: TestClient) -> None:
        """A well-behaved 401 advertises the scheme."""
        response = anon_client.post("/v1/predict", json=PREDICT_BODY)
        assert response.headers["WWW-Authenticate"] == "Bearer"

    def test_bearer_scheme_is_case_insensitive(self, anon_client: TestClient) -> None:
        """RFC 7235 requires the scheme to be matched case-insensitively."""
        response = anon_client.post(
            "/v1/predict",
            json=PREDICT_BODY,
            headers={"Authorization": f"bEaReR {TEST_API_KEY}"},
        )
        assert response.status_code == 200

    def test_dog_endpoint_requires_auth(self, anon_client: TestClient) -> None:
        """The robot-dog route is protected too."""
        response = anon_client.post("/v1/robot-dog/localization-reliability", json=DOG_BODY)
        assert response.status_code == 401


class TestPredictEndpoint:
    """``POST /v1/predict``."""

    def test_happy_path_returns_200(self, client: TestClient) -> None:
        """An authenticated request succeeds."""
        assert client.post("/v1/predict", json=PREDICT_BODY).status_code == 200

    def test_response_envelope_is_complete(self, client: TestClient) -> None:
        """The response carries ok, data, and meta."""
        body = client.post("/v1/predict", json=PREDICT_BODY).json()
        assert body["ok"] is True
        assert "answers" in body["data"]
        assert set(body["meta"]) >= {"backend", "model", "request_id"}

    def test_answer_is_in_laya_wire_shape(self, client: TestClient) -> None:
        """The answer keeps the {type, <type>} structure Laya itself returns.

        A ``noul`` answer is the *probability that the answer is true*, not a
        boolean -- that is Laya's real contract, and the service forwards it
        unchanged rather than pretending otherwise.
        """
        answer = client.post("/v1/predict", json=PREDICT_BODY).json()["data"]["answers"][
            "location_reliable"
        ]
        assert answer["type"] == "noul"
        assert isinstance(answer["noul"], float)
        assert 0.0 <= answer["noul"] <= 1.0
        assert answer["noul"] == pytest.approx(0.9)

    def test_meta_reports_the_model(self, client: TestClient) -> None:
        """Provenance metadata identifies the serving model."""
        meta = client.post("/v1/predict", json=PREDICT_BODY).json()["meta"]
        assert meta["backend"] == "fake"
        assert meta["model"] == "fake-model-v1"

    def test_meta_reports_latency(self, client: TestClient) -> None:
        """A latency figure is reported for observability."""
        meta = client.post("/v1/predict", json=PREDICT_BODY).json()["meta"]
        assert isinstance(meta["latency_ms"], (int, float))
        assert meta["latency_ms"] >= 0

    def test_rejects_empty_questions(self, client: TestClient) -> None:
        """A request with no questions is a validation error."""
        response = client.post("/v1/predict", json={"state": {}, "questions": {}})
        assert response.status_code == 422

    def test_rejects_unknown_question_type(self, client: TestClient) -> None:
        """An unsupported question type is rejected at the schema layer."""
        response = client.post(
            "/v1/predict",
            json={
                "state": {},
                "questions": {"q": {"type": "telepathy", "instructions": "?"}},
            },
        )
        assert response.status_code == 422

    def test_rejects_question_without_instructions(self, client: TestClient) -> None:
        """Instructions are mandatory."""
        response = client.post(
            "/v1/predict", json={"state": {}, "questions": {"q": {"type": "noul"}}}
        )
        assert response.status_code == 422

    def test_rejects_extra_top_level_fields(self, client: TestClient) -> None:
        """``extra='forbid'`` stops silent typos becoming silent no-ops."""
        response = client.post("/v1/predict", json={**PREDICT_BODY, "unexpected": "field"})
        assert response.status_code == 422

    def test_rejects_missing_state(self, client: TestClient) -> None:
        """State is required."""
        response = client.post("/v1/predict", json={"questions": PREDICT_BODY["questions"]})
        assert response.status_code == 422

    def test_validation_error_uses_the_envelope(self, client: TestClient) -> None:
        """Validation failures share the error envelope."""
        body = client.post("/v1/predict", json={"state": {}, "questions": {}}).json()
        assert body["ok"] is False
        assert body["error"]["code"] == "validation_error"

    def test_model_load_failure_is_503(self, app, client: TestClient) -> None:  # type: ignore[no-untyped-def]
        """A model that cannot load maps to 503, not 500."""
        from laya_service.interfaces.http.dependencies import get_decision_model

        broken = FakeDecisionModel()
        broken.raise_on_predict = ModelLoadError("weights unavailable")
        app.dependency_overrides[get_decision_model] = lambda: broken

        response = client.post("/v1/predict", json=PREDICT_BODY)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "model_load_failed"

    def test_inference_failure_is_503(self, app, client: TestClient) -> None:  # type: ignore[no-untyped-def]
        """An inference failure maps to 503 as well."""
        from laya_service.interfaces.http.dependencies import get_decision_model

        broken = FakeDecisionModel()
        broken.raise_on_predict = ModelInferenceError("oom")
        app.dependency_overrides[get_decision_model] = lambda: broken

        response = client.post("/v1/predict", json=PREDICT_BODY)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "model_inference_failed"

    def test_503_does_not_leak_internals(self, app, client: TestClient) -> None:  # type: ignore[no-untyped-def]
        """The error body carries no traceback or file path."""
        from laya_service.interfaces.http.dependencies import get_decision_model

        broken = FakeDecisionModel()
        broken.raise_on_predict = ModelInferenceError("secret internal detail")
        app.dependency_overrides[get_decision_model] = lambda: broken

        text = client.post("/v1/predict", json=PREDICT_BODY).text
        assert "Traceback" not in text
        assert ".py" not in text


class TestRobotDogEndpoint:
    """``POST /v1/robot-dog/localization-reliability``."""

    def test_happy_path_returns_200(self, client: TestClient) -> None:
        """The canonical example request succeeds."""
        assert (
            client.post("/v1/robot-dog/localization-reliability", json=DOG_BODY).status_code == 200
        )

    def test_response_contains_reliability_and_recommendation(self, client: TestClient) -> None:
        """Both documented output fields are present."""
        data = client.post("/v1/robot-dog/localization-reliability", json=DOG_BODY).json()["data"]
        assert "reliability" in data
        assert "recommendation" in data
        assert 0.0 <= data["reliability"] <= 1.0

    def test_unreliable_verdict_recommends_switching(self, app, client: TestClient) -> None:  # type: ignore[no-untyped-def]
        """A confident 'unreliable' answer maps to the switch recommendation."""
        from laya_service.interfaces.http.dependencies import get_decision_model

        app.dependency_overrides[get_decision_model] = lambda: FakeDecisionModel(
            probability_true=0.05, confidence=0.95
        )
        data = client.post("/v1/robot-dog/localization-reliability", json=DOG_BODY).json()["data"]
        assert data["reliable"] is False
        assert data["recommendation"] == "switch_to_visual_imu"

    def test_low_confidence_falls_back_conservatively(self, app, client: TestClient) -> None:  # type: ignore[no-untyped-def]
        """An unconfident answer is flagged and recommends the safe action."""
        from laya_service.interfaces.http.dependencies import get_decision_model

        app.dependency_overrides[get_decision_model] = lambda: FakeDecisionModel(
            probability_true=0.9, confidence=0.1
        )
        data = client.post("/v1/robot-dog/localization-reliability", json=DOG_BODY).json()["data"]
        assert data["confident"] is False
        assert data["recommendation"] == "switch_to_visual_imu"

    def test_custom_threshold_is_honoured(self, client: TestClient) -> None:
        """The caller can raise the confidence bar."""
        body = {**DOG_BODY, "confidence_threshold": 0.99}
        data = client.post("/v1/robot-dog/localization-reliability", json=body).json()["data"]
        assert data["confident"] is False

    def test_rejects_negative_jump(self, client: TestClient) -> None:
        """A negative distance fails schema validation."""
        response = client.post(
            "/v1/robot-dog/localization-reliability", json={**DOG_BODY, "x_jump_m": -1.0}
        )
        assert response.status_code == 422

    def test_rejects_out_of_range_confidence(self, client: TestClient) -> None:
        """A confidence outside [0, 1] is rejected."""
        response = client.post(
            "/v1/robot-dog/localization-reliability",
            json={**DOG_BODY, "confidence_start": 1.5},
        )
        assert response.status_code == 422

    def test_rejects_empty_environment(self, client: TestClient) -> None:
        """An empty environment tag is rejected."""
        response = client.post(
            "/v1/robot-dog/localization-reliability", json={**DOG_BODY, "environment": ""}
        )
        assert response.status_code == 422

    def test_rejects_extra_fields(self, client: TestClient) -> None:
        """Extra fields are refused."""
        response = client.post(
            "/v1/robot-dog/localization-reliability", json={**DOG_BODY, "bogus": 1}
        )
        assert response.status_code == 422

    def test_meta_is_present(self, client: TestClient) -> None:
        """Provenance metadata accompanies the payload."""
        body = client.post("/v1/robot-dog/localization-reliability", json=DOG_BODY).json()
        assert body["meta"]["model"] == "fake-model-v1"


class TestRequestId:
    """Correlation-id handling."""

    def test_generates_a_request_id(self, client: TestClient) -> None:
        """A request id is returned even when the caller supplies none."""
        assert client.get("/healthz").headers.get("X-Request-ID")

    def test_echoes_a_supplied_request_id(self, client: TestClient) -> None:
        """A caller-supplied id is honoured so traces span hops."""
        response = client.get("/healthz", headers={"X-Request-ID": "abc-123"})
        assert response.headers["X-Request-ID"] == "abc-123"

    def test_rejects_a_malicious_request_id(self, client: TestClient) -> None:
        """A header-injection attempt is replaced, not reflected."""
        response = client.get("/healthz", headers={"X-Request-ID": "bad\r\nX-Injected: yes"})
        assert "\r" not in response.headers["X-Request-ID"]
        assert "X-Injected" not in response.headers

    def test_request_id_appears_in_the_response_meta(self, client: TestClient) -> None:
        """The id is threaded through to the response body."""
        response = client.post(
            "/v1/predict", json=PREDICT_BODY, headers={"X-Request-ID": "trace-me"}
        )
        assert response.json()["meta"]["request_id"] == "trace-me"

    def test_request_id_present_on_auth_failure(self, anon_client: TestClient) -> None:
        """Even a rejected request carries a correlation id."""
        response = anon_client.post("/v1/predict", json=PREDICT_BODY)
        assert response.headers.get("X-Request-ID")

    def test_trace_id_header_is_returned(self, client: TestClient) -> None:
        """The trace id header accompanies the request id."""
        assert client.get("/healthz").headers.get("X-Trace-ID")


class TestOpenApi:
    """The generated schema."""

    def test_openapi_is_served(self, anon_client: TestClient) -> None:
        """The schema is available without credentials."""
        assert anon_client.get("/openapi.json").status_code == 200

    def test_documented_paths_are_present(self, anon_client: TestClient) -> None:
        """Every public route appears in the schema."""
        paths = anon_client.get("/openapi.json").json()["paths"]
        assert "/healthz" in paths
        assert "/readyz" in paths
        assert "/v1/predict" in paths
        assert "/v1/robot-dog/localization-reliability" in paths

    def test_docs_are_served(self, anon_client: TestClient) -> None:
        """Swagger UI is reachable."""
        assert anon_client.get("/docs").status_code == 200


class TestErrorEnvelope:
    """Every error path shares one shape."""

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/does-not-exist"),
            ("post", "/v1/predict"),
        ],
    )
    def test_errors_use_the_envelope(self, anon_client: TestClient, method: str, path: str) -> None:
        """404s and 401s alike use ok/error."""
        response = getattr(anon_client, method)(path, **({"json": {}} if method == "post" else {}))
        body = response.json()
        assert body["ok"] is False
        assert "code" in body["error"]
        assert "message" in body["error"]

    def test_not_found_is_404_when_authenticated(self, client: TestClient) -> None:
        """An authenticated request to an unknown path returns 404 in the envelope."""
        response = client.get("/nope")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    def test_unknown_path_is_401_when_unauthenticated(self, anon_client: TestClient) -> None:
        """Authentication runs before routing, so unknown paths 401 first.

        This is deliberate rather than incidental: authenticating before the
        router means an unauthenticated caller cannot probe which paths exist by
        diffing 404 against 401. The cost is that a legitimate client with a bad
        token gets 401 rather than 404 for a typo'd URL, which is a fair trade.
        """
        response = anon_client.get("/nope")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"
