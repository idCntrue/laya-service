"""OpenAI-compatible endpoints.

Lets an unmodified OpenAI SDK -- or anything built on one -- point its
``base_url`` at this service. What it can do here is **tool calling**, not chat:
the caller describes the decisions they want with a tool's ``parameters`` schema
and receives them as the tool call's arguments.

That restriction is the whole design. This model classifies; it does not
generate. A request that expects prose is rejected with 400 rather than answered
with something that looks like text and is not, because a caller cannot tell the
difference and would build on it.

Wire-format mapping lives here; the semantics live in
:class:`~laya_service.application.use_cases.answer_tool_call.AnswerToolCallUseCase`.
"""

from __future__ import annotations

import json
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
from laya_service.interfaces.http.schemas.compat import OpenAIChatRequest
from laya_service.logging_config import get_logger

__all__ = ["router"]

_logger = get_logger(__name__, component="openai_compat")

router = APIRouter(tags=["openai-compatible"])

#: Error codes the OpenAI SDK understands. Emitted in the ``error.type`` field.
_ERROR_TYPE_BY_STATUS: Final[dict[int, str]] = {
    400: "invalid_request_error",
    401: "invalid_request_error",
    403: "invalid_request_error",
    404: "invalid_request_error",
    422: "invalid_request_error",
    500: "server_error",
    503: "server_error",
}


def _error(status: int, message: str, code: str) -> JSONResponse:
    """Build an OpenAI-shaped error response.

    The OpenAI SDK parses ``{"error": {"message", "type", "code"}}``. Returning
    this service's own envelope would make every failure surface as an opaque
    parse error in the client, so the compat routes translate.

    Args:
        status: The HTTP status.
        message: The human-readable message.
        code: A stable machine-readable code.

    Returns:
        The JSON response.
    """
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "message": message,
                "type": _ERROR_TYPE_BY_STATUS.get(status, "api_error"),
                "code": code,
                "param": None,
            }
        },
    )


def _select_tool(body: OpenAIChatRequest) -> tuple[str, dict[str, Any]] | JSONResponse:
    """Work out which tool's schema to answer, or explain why we cannot.

    The model cannot choose between tools -- that is a generation task -- so the
    caller must name one. Silently picking the first would produce an answer to
    a question they did not ask.

    Args:
        body: The parsed request.

    Returns:
        Either a ``(name, parameters)`` pair, or a ready error response.
    """
    tools = body.tools or []
    if not tools:
        return _error(
            400,
            "This service answers tool calls; it does not generate text. Supply a "
            "'tools' array whose function parameters describe the decisions you "
            "want, and set 'tool_choice' to name it.",
            "no_tools",
        )

    choice = body.tool_choice
    if isinstance(choice, dict):
        function = choice.get("function")
        wanted = function.get("name") if isinstance(function, dict) else None
        if not isinstance(wanted, str):
            return _error(
                400,
                "tool_choice must be {'type': 'function', 'function': {'name': ...}}",
                "invalid_tool_choice",
            )
        for tool in tools:
            if tool.function.name == wanted:
                return wanted, tool.function.parameters
        return _error(400, f"tool_choice names {wanted!r}, which is not in 'tools'", "unknown_tool")

    if isinstance(choice, str) and choice == "none":
        return _error(
            400,
            "tool_choice='none' asks for a text answer, which this model cannot produce.",
            "no_tools",
        )

    # 'auto' or absent: only unambiguous when exactly one tool was supplied.
    if len(tools) == 1:
        return tools[0].function.name, tools[0].function.parameters

    return _error(
        400,
        f"{len(tools)} tools were supplied with tool_choice='auto'. This model "
        "cannot choose between tools -- set tool_choice to name one.",
        "ambiguous_tool_choice",
    )


def _build_state(body: OpenAIChatRequest) -> dict[str, Any]:
    """Render the conversation as the state the model decides about.

    The messages are passed through structurally rather than concatenated into
    one string: role structure is information, and flattening it loses the
    distinction between what the user said and what the assistant said.

    Args:
        body: The parsed request.

    Returns:
        A state mapping.
    """
    turns = [
        {"role": message.role, "content": message.content}
        for message in body.messages
        if message.content
    ]
    if len(turns) == 1:
        # A single turn reads better as a plain observation than as a
        # one-element conversation, and matches the native API's shape.
        return {"message": turns[0]["content"]}
    return {"messages": turns}


@router.post("/v1/chat/completions")
async def chat_completions(
    body: OpenAIChatRequest,
    use_case: AnswerToolCallUseCase = Depends(get_answer_tool_call_use_case),
    settings: Any = Depends(get_settings_dep),
) -> JSONResponse:
    """Answer a tool call in the OpenAI wire format.

    Args:
        body: The parsed request.
        use_case: The injected use case.
        settings: The resolved settings, for the served-model list.

    Returns:
        A ``chat.completion`` carrying one ``tool_calls`` entry, or an
        OpenAI-shaped error.
    """
    if body.stream:
        return _error(
            400,
            "Streaming is not supported: this model produces every answer in a "
            "single forward pass, so there is nothing to stream incrementally.",
            "streaming_unsupported",
        )
    if body.n > 1:
        return _error(
            400,
            f"n={body.n} is not supported: one forward pass yields exactly one "
            "answer set, so returning fewer choices than asked would be silent.",
            "multiple_choices_unsupported",
        )

    served = settings.compat_model_list
    if body.model not in served:
        return _error(
            400,
            f"unknown model {body.model!r}. This service serves: {served}",
            "unsupported_model",
        )

    selected = _select_tool(body)
    if isinstance(selected, JSONResponse):
        return selected
    tool_name, parameters = selected

    started = time.perf_counter()
    result = use_case.execute(
        CompatRequest(
            state=_build_state(body),
            tool_name=tool_name,
            schema=parameters,
            model=body.model,
        )
    )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)

    return JSONResponse(
        content={
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": result.model_name,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"call_{uuid.uuid4().hex[:24]}",
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    # The SDK parses this string, so it must be
                                    # valid JSON -- which is also why the
                                    # response schema is `str`, not an object.
                                    "arguments": json.dumps(result.arguments),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            # The OpenAI SDK requires a usage object on every response.
            "usage": {
                "prompt_tokens": _estimate_tokens(body),
                "completion_tokens": 0,
                "total_tokens": _estimate_tokens(body),
            },
            # Vendor metadata under a namespaced key, so a client that ignores it
            # is unaffected and one that reads it can see how the answer was
            # produced.
            "_laya": {
                "backend": result.backend,
                "model": result.model_name,
                "latency_ms": result.latency_ms,
                "server_latency_ms": elapsed_ms,
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


@router.get("/v1/models")
async def list_models(settings: Any = Depends(get_settings_dep)) -> JSONResponse:
    """List the model identifiers this service serves.

    Args:
        settings: The resolved settings.

    Returns:
        An OpenAI-shaped model list. Every required field is present -- a strict
        client raises a validation error if ``owned_by`` is missing.
    """
    created = 0
    return JSONResponse(
        content={
            "object": "list",
            "data": [
                {
                    "id": name,
                    "object": "model",
                    "created": created,
                    "owned_by": "laya-service",
                }
                for name in settings.compat_model_list
            ],
        }
    )


def _estimate_tokens(body: OpenAIChatRequest) -> int:
    """Estimate the prompt size for the usage block.

    A rough character-based proxy, not a real tokenizer: the interface layer may
    not import ``transformers``, and the value is informational. It is reported
    as ``prompt_tokens`` because the OpenAI SDK requires the field, and the
    vendor block carries the note that it is an estimate.

    Args:
        body: The parsed request.

    Returns:
        An estimated token count.
    """
    characters = sum(len(message.content or "") for message in body.messages)
    return max(1, characters // 4)
