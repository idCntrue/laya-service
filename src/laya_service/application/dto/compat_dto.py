"""Data transfer objects for the OpenAI / Anthropic compatibility layer.

Framework-free, like every other DTO here. The compat routes do the wire-format
work -- parsing an OpenAI ``tools`` array or an Anthropic ``input_schema`` -- and
hand the result across as these types. The use case never sees a Pydantic model,
and the routes never see the model port.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["CompatRequest", "CompatResult"]


@dataclass(frozen=True, slots=True)
class CompatRequest:
    """A tool call to answer, already reduced to its essentials.

    Attributes:
        state: The situation to decide about, as a mapping. Built by the route
            from the request's messages.
        tool_name: The tool the caller forced, for the response envelope.
        schema: The tool's ``input_schema`` / ``parameters``.
        model: The requested model identifier, or empty for the default.
    """

    state: Mapping[str, Any]
    tool_name: str
    schema: Mapping[str, Any]
    model: str = ""


@dataclass(frozen=True, slots=True)
class CompatResult:
    """The answer to a tool call.

    Attributes:
        arguments: The JSON object the tool call should carry.
        model_name: The model that served the request, for the response envelope.
        backend: The compute backend that served the request.
        thresholds: Property name to the threshold used for a ``boolean``
            property, so the response can record which values were derived
            rather than returned directly.
        latency_ms: Server-side handling time.
    """

    arguments: Mapping[str, Any] = field(default_factory=dict)
    model_name: str = ""
    backend: str = ""
    thresholds: Mapping[str, float] = field(default_factory=dict)
    latency_ms: float = 0.0
