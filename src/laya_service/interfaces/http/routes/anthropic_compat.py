"""Anthropic-compatible endpoint.

Lets an unmodified Anthropic SDK point its ``base_url`` at this service and use
**tool use**. The caller describes the decisions they want with a tool's
``input_schema``; the answer arrives as a ``tool_use`` content block whose
``input`` is the resulting object.

As with the OpenAI surface, text generation is refused rather than faked. The
model classifies.

The Anthropic wire format differs from OpenAI's in ways that matter here:

* the credential is ``x-api-key``, not ``Authorization: Bearer`` (the auth
  middleware accepts both);
* the error envelope is ``{"type": "error", "error": {...}}``;
* ``max_tokens`` is required by the SDK but meaningless to a classifier;
* ``usage`` carries only ``input_tokens`` and ``output_tokens`` -- adding a
  ``total_tokens`` field would be a wire-format violation.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Final

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from laya_service.application.dto.compat_dto import CompatRequest
from laya_service.application.use_cases.answer_tool_call import AnswerToolCallUseCase
from laya_service.interfaces.http.dependencies import (
    get_answer_tool_call_use_case,
    get_settings_dep,
)
from laya_service.interfaces.http.schemas.compat import AnthropicMessagesRequest
from laya_service.logging_config import get_logger

__all__ = ["router"]

_logger = get_logger(__name__, component="anthropic_compat")

router = APIRouter(tags=["anthropic-compatible"])

#: Anthropic error types, keyed by HTTP status.
_ERROR_TYPE_BY_STATUS: Final[dict[int, str]] = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    500: "api_error",
    503: "overloaded_error",
}


def _error(status: int, message: str) -> JSONResponse:
    """Build an Anthropic-shaped error response.

    The SDK parses ``{"type": "error", "error": {"type", "message"}}``. This
    service's own envelope would surface as an unhelpful parse failure.

    Args:
        status: The HTTP status.
        message: The human-readable message.

    Returns:
        The JSON response.
    """
    return JSONResponse(
        status_code=status,
        content={
            "type": "error",
            "error": {
                "type": _ERROR_TYPE_BY_STATUS.get(status, "api_error"),
                "message": message,
            },
        },
    )


def _select_tool(body: AnthropicMessagesRequest) -> tuple[str, dict[str, Any]] | JSONResponse:
    """Work out which tool's schema to answer, or explain why we cannot.

    Args:
        body: The parsed request.

    Returns:
        Either a ``(name, input_schema)`` pair, or a ready error response.
    """
    tools = body.tools or []
    if not tools:
        return _error(
            400,
            "This service answers tool calls; it does not generate text. Supply a "
            "'tools' array whose input_schema describes the decisions you want, and "
            "set tool_choice to name one.",
        )

    choice = body.tool_choice
    if isinstance(choice, dict):
        wanted = choice.get("name")
        if not isinstance(wanted, str):
            return _error(400, "tool_choice must be {'type': 'tool', 'name': ...}")
        for tool in tools:
            if tool.get("name") == wanted:
                schema = tool.get("input_schema")
                if not isinstance(schema, dict):
                    return _error(400, f"tool {wanted!r} has no 'input_schema' object")
                return wanted, schema
        return _error(400, f"tool_choice names {wanted!r}, which is not in 'tools'")

    # 'auto' / 'any' / absent: unambiguous only when exactly one tool is present.
    if len(tools) == 1:
        name = tools[0].get("name")
        schema = tools[0].get("input_schema")
        if not isinstance(name, str) or not isinstance(schema, dict):
            return _error(400, "the supplied tool needs a 'name' and an 'input_schema'")
        return name, schema

    return _error(
        400,
        f"{len(tools)} tools were supplied without naming one in tool_choice. This "
        "model cannot choose between tools -- set tool_choice to name one.",
    )


def _build_state(body: AnthropicMessagesRequest) -> dict[str, Any]:
    """Render the conversation as the state the model decides about.

    Anthropic content may be a string or a list of blocks; both are reduced to
    text here, since the model reads prose.

    Args:
        body: The parsed request.

    Returns:
        A state mapping.
    """
    turns: list[dict[str, Any]] = []
    for message in body.messages:
        text = _flatten_content(message.content)
        if text:
            turns.append({"role": message.role, "content": text})

    if len(turns) == 1:
        return {"message": turns[0]["content"]}
    return {"messages": turns}


def _flatten_content(content: Any) -> str:
    """Reduce an Anthropic content field to plain text.

    Args:
        content: A string, a list of content blocks, or ``None``.

    Returns:
        The concatenated text, or an empty string when there is none.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return ""


@router.post("/v1/messages")
async def create_message(
    body: AnthropicMessagesRequest,
    use_case: AnswerToolCallUseCase = Depends(get_answer_tool_call_use_case),
    settings: Any = Depends(get_settings_dep),
) -> JSONResponse:
    """Answer a tool use request in the Anthropic wire format.

    Args:
        body: The parsed request.
        use_case: The injected use case.
        settings: The resolved settings, for the served-model list.

    Returns:
        A ``message`` carrying one ``tool_use`` block, or an Anthropic-shaped
        error.
    """
    if body.stream:
        return _error(
            400,
            "Streaming is not supported: this model produces every answer in a "
            "single forward pass, so there is nothing to stream incrementally.",
        )

    served = settings.compat_model_list
    if body.model not in served:
        return _error(400, f"unknown model {body.model!r}. This service serves: {served}")

    selected = _select_tool(body)
    if isinstance(selected, JSONResponse):
        return selected
    tool_name, schema = selected

    result = use_case.execute(
        CompatRequest(
            state=_build_state(body),
            tool_name=tool_name,
            schema=schema,
            model=body.model,
        )
    )

    prompt_tokens = _estimate_tokens(body)
    return JSONResponse(
        content={
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "type": "message",
            "role": "assistant",
            "model": result.model_name,
            "content": [
                {
                    "type": "tool_use",
                    "id": f"toolu_{uuid.uuid4().hex[:24]}",
                    "name": tool_name,
                    # Anthropic's tool_use carries the input as an object, not a
                    # JSON string -- the opposite of OpenAI's arguments field.
                    "input": dict(result.arguments),
                }
            ],
            # A client branching on stop_reason == "tool_use" is the normal path,
            # so this must be set even though nothing was generated.
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "usage": {
                "input_tokens": prompt_tokens,
                "output_tokens": 0,
            },
            "_laya": {
                "backend": result.backend,
                "latency_ms": result.latency_ms,
                "derived_thresholds": dict(result.thresholds),
                "note": (
                    "Probabilities from this model are not calibrated. Boolean "
                    "properties were derived at the thresholds listed in "
                    "'derived_thresholds'; declare a number property to receive "
                    "the probability itself."
                ),
            },
        }
    )


def _estimate_tokens(body: AnthropicMessagesRequest) -> int:
    """Estimate the prompt size for the usage block.

    A character-based proxy, not a real tokenizer: the interface layer may not
    import ``transformers``. Anthropic requires the field, so it is always
    reported.

    Args:
        body: The parsed request.

    Returns:
        An estimated token count.
    """
    characters = sum(len(_flatten_content(message.content)) for message in body.messages)
    return max(1, characters // 4)


_ = time  # kept for symmetry with the OpenAI route's timestamp usage
