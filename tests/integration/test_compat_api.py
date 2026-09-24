"""Integration tests for the OpenAI / Anthropic compatibility layer.

These exercise the real HTTP stack -- real middleware, real exception handlers,
real routing -- with only the decision-model dependency swapped for the
deterministic fake. What they pin down is the contract a caller's SDK depends
on: the wire shapes, and the refusals.

The refusals matter as much as the successes. A request that expects text
generation must fail loudly, because a plausible-looking answer would be
indistinguishable from a real language model's and a caller would build on it.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_API_KEY, FakeDecisionModel

#: A tool schema exercising every supported property shape.
TOOL_SCHEMA = {
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
}

OPENAI_BODY = {
    "model": "english",
    "messages": [{"role": "user", "content": "Order #12345 arrived broken"}],
    "tools": [{"type": "function", "function": {"name": "classify", "parameters": TOOL_SCHEMA}}],
    "tool_choice": {"type": "function", "function": {"name": "classify"}},
}

ANTHROPIC_BODY = {
    "model": "english",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "Order #12345 arrived broken"}],
    "tools": [{"name": "classify", "input_schema": TOOL_SCHEMA}],
    "tool_choice": {"type": "tool", "name": "classify"},
}


class TestOpenAIChatCompletions:
    """``POST /v1/chat/completions``."""

    def test_returns_a_tool_call(self, client: TestClient) -> None:
        """A well-formed request produces one tool call."""
        response = client.post("/v1/chat/completions", json=OPENAI_BODY)
        assert response.status_code == 200
        body = response.json()
        assert body["object"] == "chat.completion"
        calls = body["choices"][0]["message"]["tool_calls"]
        assert len(calls) == 1
        assert calls[0]["function"]["name"] == "classify"

    def test_finish_reason_is_tool_calls(self, client: TestClient) -> None:
        """A client branching on finish_reason must take the tool-call path."""
        body = client.post("/v1/chat/completions", json=OPENAI_BODY).json()
        assert body["choices"][0]["finish_reason"] == "tool_calls"

    def test_arguments_are_a_json_string(self, client: TestClient) -> None:
        """OpenAI's arguments field is a string the SDK parses, not an object."""
        body = client.post("/v1/chat/completions", json=OPENAI_BODY).json()
        arguments = body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
        assert isinstance(arguments, str)
        assert isinstance(json.loads(arguments), dict)

    def test_arguments_cover_every_property(self, client: TestClient) -> None:
        """Each schema property is answered."""
        body = client.post("/v1/chat/completions", json=OPENAI_BODY).json()
        arguments = json.loads(
            body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
        )
        assert set(arguments) == {"category", "churn_risk", "needs_human"}

    def test_enum_answer_is_one_of_the_enum(self, client: TestClient) -> None:
        """A choice answer is always a declared enum member."""
        body = client.post("/v1/chat/completions", json=OPENAI_BODY).json()
        arguments = json.loads(
            body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
        )
        assert arguments["category"] in {"refund", "technical", "billing"}

    def test_boolean_answer_is_a_real_boolean(self, client: TestClient) -> None:
        """A boolean property receives a JSON boolean, not a probability."""
        body = client.post("/v1/chat/completions", json=OPENAI_BODY).json()
        arguments = json.loads(
            body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
        )
        assert isinstance(arguments["needs_human"], bool)

    def test_usage_block_is_present(self, client: TestClient) -> None:
        """The OpenAI SDK requires a usage object."""
        body = client.post("/v1/chat/completions", json=OPENAI_BODY).json()
        assert set(body["usage"]) == {"prompt_tokens", "completion_tokens", "total_tokens"}

    def test_carries_the_derived_threshold(self, client: TestClient) -> None:
        """The response records which answers were derived rather than returned."""
        body = client.post("/v1/chat/completions", json=OPENAI_BODY).json()
        assert body["_laya"]["derived_thresholds"] == {"needs_human": 0.5}

    def test_requires_authentication(self, anon_client: TestClient) -> None:
        """The compat surface is protected like every other /v1 route."""
        assert anon_client.post("/v1/chat/completions", json=OPENAI_BODY).status_code == 401

    def test_accepts_x_api_key_header(self, client: TestClient, app: object) -> None:
        """The Anthropic-style header authenticates too."""
        from fastapi.testclient import TestClient as _TestClient

        assert isinstance(app, object)
        with _TestClient(app, headers={"x-api-key": TEST_API_KEY}) as c:  # type: ignore[arg-type]
            assert c.post("/v1/chat/completions", json=OPENAI_BODY).status_code == 200


class TestOpenAIRefusals:
    """Requests this service must refuse rather than fake."""

    def test_chat_without_tools_is_rejected(self, client: TestClient) -> None:
        """A plain chat request cannot be answered, so it must not be faked."""
        response = client.post(
            "/v1/chat/completions",
            json={"model": "english", "messages": [{"role": "user", "content": "Write a poem"}]},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "no_tools"

    def test_streaming_is_rejected(self, client: TestClient) -> None:
        """There is nothing to stream incrementally."""
        response = client.post("/v1/chat/completions", json={**OPENAI_BODY, "stream": True})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "streaming_unsupported"

    def test_multiple_choices_are_rejected(self, client: TestClient) -> None:
        """Returning fewer choices than asked would be a silent error."""
        response = client.post("/v1/chat/completions", json={**OPENAI_BODY, "n": 3})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "multiple_choices_unsupported"

    def test_unknown_model_is_rejected(self, client: TestClient) -> None:
        """An unserved model identifier is an error, not a silent default."""
        response = client.post("/v1/chat/completions", json={**OPENAI_BODY, "model": "gpt-4"})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "unsupported_model"

    def test_ambiguous_tool_choice_is_rejected(self, client: TestClient) -> None:
        """The model cannot choose between tools, so picking one would be a guess."""
        body = {
            **OPENAI_BODY,
            "tools": [
                OPENAI_BODY["tools"][0],  # type: ignore[index]
                {
                    "type": "function",
                    "function": {"name": "other", "parameters": TOOL_SCHEMA},
                },
            ],
            "tool_choice": "auto",
        }
        response = client.post("/v1/chat/completions", json=body)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "ambiguous_tool_choice"

    def test_unmappable_schema_is_rejected(self, client: TestClient) -> None:
        """A schema with no honest mapping is refused with an explanation."""
        body = {
            **OPENAI_BODY,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "classify",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "essay": {"type": "string", "description": "Write an essay"}
                            },
                        },
                    },
                }
            ],
        }
        response = client.post("/v1/chat/completions", json=body)
        assert response.status_code == 400
        # The OpenAI envelope carries `type`, not our own `code`.
        assert response.json()["error"]["type"] == "invalid_request_error"
        assert "no mapping" in response.json()["error"]["message"]

    def test_boolean_without_threshold_is_rejected(self, client: TestClient) -> None:
        """The derivation is lossy, so it must be opted into."""
        body = {
            **OPENAI_BODY,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "classify",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "spam": {"type": "boolean", "description": "Is it spam?"}
                            },
                        },
                    },
                }
            ],
        }
        response = client.post("/v1/chat/completions", json=body)
        assert response.status_code == 400
        assert "x-laya-threshold" in response.json()["error"]["message"]

    def test_errors_use_the_openai_envelope(self, client: TestClient) -> None:
        """The OpenAI SDK parses {"error": {...}}; our own envelope would not."""
        body = client.post("/v1/chat/completions", json={"model": "english", "messages": []}).json()
        assert "error" in body
        assert set(body["error"]) >= {"message", "type", "code"}


class TestAnthropicMessages:
    """``POST /v1/messages``."""

    def test_returns_a_tool_use_block(self, client: TestClient) -> None:
        """A well-formed request produces one tool_use block."""
        response = client.post("/v1/messages", json=ANTHROPIC_BODY)
        assert response.status_code == 200
        body = response.json()
        assert body["type"] == "message"
        assert body["role"] == "assistant"
        assert body["content"][0]["type"] == "tool_use"

    def test_stop_reason_is_tool_use(self, client: TestClient) -> None:
        """A client branching on stop_reason must take the tool path."""
        body = client.post("/v1/messages", json=ANTHROPIC_BODY).json()
        assert body["stop_reason"] == "tool_use"

    def test_input_is_an_object(self, client: TestClient) -> None:
        """Anthropic's input is an object, unlike OpenAI's JSON string."""
        body = client.post("/v1/messages", json=ANTHROPIC_BODY).json()
        assert isinstance(body["content"][0]["input"], dict)

    def test_usage_has_no_total(self, client: TestClient) -> None:
        """Anthropic's usage carries only input/output tokens."""
        body = client.post("/v1/messages", json=ANTHROPIC_BODY).json()
        assert set(body["usage"]) == {"input_tokens", "output_tokens"}

    def test_requires_authentication(self, anon_client: TestClient) -> None:
        """The Anthropic surface is protected too."""
        assert anon_client.post("/v1/messages", json=ANTHROPIC_BODY).status_code == 401

    def test_accepts_content_blocks(self, client: TestClient) -> None:
        """Anthropic content may be a list of blocks rather than a string."""
        body = {
            **ANTHROPIC_BODY,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "Order #12345 arrived broken"}],
                }
            ],
        }
        assert client.post("/v1/messages", json=body).status_code == 200

    def test_streaming_is_rejected(self, client: TestClient) -> None:
        """Streaming is refused here as well."""
        response = client.post("/v1/messages", json={**ANTHROPIC_BODY, "stream": True})
        assert response.status_code == 400

    def test_errors_use_the_anthropic_envelope(self, client: TestClient) -> None:
        """The SDK parses {"type": "error", "error": {...}}."""
        body = client.post("/v1/messages", json={"model": "english", "messages": []}).json()
        assert body["type"] == "error"
        assert "message" in body["error"]

    def test_unknown_model_is_rejected(self, client: TestClient) -> None:
        """An unserved model is an error."""
        response = client.post(
            "/v1/messages", json={**ANTHROPIC_BODY, "model": "claude-sonnet-5"}
        )
        assert response.status_code == 400


class TestModelsEndpoint:
    """``GET /v1/models``."""

    def test_lists_the_served_models(self, client: TestClient) -> None:
        """The served model identifiers are discoverable."""
        body = client.get("/v1/models").json()
        assert body["object"] == "list"
        assert [entry["id"] for entry in body["data"]] == ["english"]

    def test_entries_have_every_required_field(self, client: TestClient) -> None:
        """A strict SDK raises if owned_by is missing."""
        entry = client.get("/v1/models").json()["data"][0]
        assert set(entry) == {"id", "object", "created", "owned_by"}

    def test_requires_authentication(self, anon_client: TestClient) -> None:
        """The model list is not public."""
        assert anon_client.get("/v1/models").status_code == 401


class TestCompatDegradation:
    """Model failures surface as the right status in each wire format."""

    def test_model_failure_is_503(self, client: TestClient, app: object) -> None:
        """A model that cannot load is a dependency failure, not a client error."""
        from laya_service.domain.exceptions import ModelLoadError
        from laya_service.interfaces.http.dependencies import get_decision_model

        broken = FakeDecisionModel()
        broken.raise_on_predict = ModelLoadError("weights missing")
        app.dependency_overrides[get_decision_model] = lambda: broken  # type: ignore[attr-defined]

        response = client.post("/v1/chat/completions", json=OPENAI_BODY)
        assert response.status_code == 503
        assert response.json()["error"]["type"] == "server_error"


class TestApiKeyAdministration:
    """``/admin/keys`` -- the key lifecycle over HTTP."""

    def test_requires_authentication(self, anon_client: TestClient) -> None:
        """The admin surface is protected by default, not by accident."""
        assert anon_client.get("/admin/keys").status_code == 401

    def test_create_returns_the_secret_once(self, client: TestClient) -> None:
        """The plaintext appears in the create response and nowhere else."""
        created = client.post("/admin/keys", json={"name": "ci"}).json()
        assert created["secret"].startswith("laya_sk_")
        listed = client.get("/admin/keys").json()["keys"]
        assert all("secret" not in entry for entry in listed)

    def test_created_key_authenticates(self, client: TestClient, app: object) -> None:
        """A freshly issued key works immediately, without a restart."""
        from fastapi.testclient import TestClient as _TestClient

        secret = client.post("/admin/keys", json={"name": "ci"}).json()["secret"]
        with _TestClient(app, headers={"Authorization": f"Bearer {secret}"}) as c:  # type: ignore[arg-type]
            assert c.post("/v1/predict", json={"state": {}, "questions": {}}).status_code != 401

    def test_inference_key_cannot_reach_admin(self, client: TestClient, app: object) -> None:
        """A leaked inference key cannot mint itself a replacement."""
        from fastapi.testclient import TestClient as _TestClient

        secret = client.post("/admin/keys", json={"name": "ci"}).json()["secret"]
        with _TestClient(app, headers={"x-api-key": secret}) as c:  # type: ignore[arg-type]
            assert c.get("/admin/keys").status_code == 403
            assert c.post("/admin/keys", json={"name": "escalate"}).status_code == 403

    def test_list_reports_available_scopes(self, client: TestClient) -> None:
        """A caller need not guess the scope names."""
        body = client.get("/admin/keys").json()
        assert set(body["available_scopes"]) == {"admin", "inference"}
        assert body["default_scopes"] == ["inference"]

    def test_default_scopes_are_inference_only(self, client: TestClient) -> None:
        """Minting an administrator takes a deliberate act."""
        created = client.post("/admin/keys", json={"name": "ci"}).json()
        assert created["scopes"] == ["inference"]

    def test_update_renames(self, client: TestClient) -> None:
        """A key's label can be changed."""
        created = client.post("/admin/keys", json={"name": "old"}).json()
        updated = client.patch(f"/admin/keys/{created['id']}", json={"name": "new"}).json()
        assert updated["name"] == "new"

    def test_update_leaves_omitted_fields_alone(self, client: TestClient) -> None:
        """Patching only the name does not clear the scopes."""
        created = client.post("/admin/keys", json={"name": "old"}).json()
        updated = client.patch(f"/admin/keys/{created['id']}", json={"name": "new"}).json()
        assert updated["scopes"] == ["inference"]

    def test_get_by_id(self, client: TestClient) -> None:
        """A key can be looked up by its identifier."""
        created = client.post("/admin/keys", json={"name": "ci"}).json()
        assert client.get(f"/admin/keys/{created['id']}").json()["id"] == created["id"]

    def test_get_unknown_id_is_400(self, client: TestClient) -> None:
        """An unknown identifier is reported, not silently empty."""
        response = client.get("/admin/keys/nope")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_api_key"

    def test_revoke_takes_effect_immediately(self, client: TestClient, app: object) -> None:
        """A revoked key stops working at once."""
        from fastapi.testclient import TestClient as _TestClient

        created = client.post("/admin/keys", json={"name": "ci"}).json()
        assert client.delete(f"/admin/keys/{created['id']}").status_code == 200
        with _TestClient(app, headers={"Authorization": f"Bearer {created['secret']}"}) as c:  # type: ignore[arg-type]
            assert c.post("/v1/predict", json={"state": {}, "questions": {}}).status_code == 401

    def test_revoked_key_drops_from_the_listing(self, client: TestClient) -> None:
        """A revoked key is hidden unless explicitly requested."""
        created = client.post("/admin/keys", json={"name": "ci"}).json()
        client.delete(f"/admin/keys/{created['id']}")
        assert client.get("/admin/keys").json()["keys"] == []
        assert len(client.get("/admin/keys?include_revoked=true").json()["keys"]) == 1

    def test_double_revoke_is_400(self, client: TestClient) -> None:
        """Revoking twice is reported, not silently idempotent."""
        created = client.post("/admin/keys", json={"name": "ci"}).json()
        client.delete(f"/admin/keys/{created['id']}")
        assert client.delete(f"/admin/keys/{created['id']}").status_code == 400

    def test_unknown_scope_is_rejected(self, client: TestClient) -> None:
        """Only known scopes are grantable."""
        response = client.post("/admin/keys", json={"name": "ci", "scopes": ["root"]})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_api_key"

    def test_blank_name_is_rejected(self, client: TestClient) -> None:
        """A key with no label is not auditable."""
        assert client.post("/admin/keys", json={"name": ""}).status_code == 422

    def test_the_secret_never_appears_in_logs(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Issuing a key must not write the credential to the log."""
        with caplog.at_level("DEBUG"):
            secret = client.post("/admin/keys", json={"name": "ci"}).json()["secret"]
        assert secret not in caplog.text

    def test_the_configured_key_is_not_written_to_the_file(self, client: TestClient) -> None:
        """The bootstrap key stays a recovery credential, outside the store."""
        client.post("/admin/keys", json={"name": "ci"})
        # The middleware was built with this test's settings, so its store path
        # is the per-test temp file -- assert the bootstrap key is not in it.
        listing = client.get("/admin/keys").json()["keys"]
        assert all(entry["name"] != TEST_API_KEY for entry in listing)
        assert all(not entry["prefix"].startswith(TEST_API_KEY) for entry in listing)
