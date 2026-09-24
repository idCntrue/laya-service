"""Request and response schemas for the OpenAI and Anthropic compatibility layer.

**``extra="allow"`` is used here, deliberately, and it is the opposite of every
other schema in this package.** Real SDKs send fields this service does not
model -- ``stream``, ``temperature``, ``top_p``, ``user``, ``metadata``,
``anthropic-version``. Rejecting them would break the compatibility promise for
every caller. They are accepted and ignored, and the ignored ones are listed
explicitly in the API documentation so a caller who sets ``temperature`` knows
it had no effect.

The exception is a field whose silent acceptance would produce a *wrong answer*
rather than an ignored preference. ``stream: true`` and ``n > 1`` are rejected
with a clear error, because honouring them is impossible and ignoring them would
return a response the caller cannot interpret.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AnthropicMessage",
    "AnthropicMessagesRequest",
    "OpenAIChatRequest",
    "OpenAIFunctionDefinition",
    "OpenAIMessage",
    "OpenAITool",
]

#: Fields an OpenAI client may send that have no meaning here. Accepted so the
#: request parses; documented so the caller knows they were ignored.
IGNORED_OPENAI_FIELDS: tuple[str, ...] = (
    "temperature",
    "top_p",
    "n",
    "stop",
    "max_tokens",
    "max_completion_tokens",
    "presence_penalty",
    "frequency_penalty",
    "logit_bias",
    "logprobs",
    "top_logprobs",
    "seed",
    "response_format",
    "user",
    "stream_options",
    "parallel_tool_calls",
)

#: The Anthropic equivalents.
IGNORED_ANTHROPIC_FIELDS: tuple[str, ...] = (
    "temperature",
    "top_p",
    "top_k",
    "metadata",
    "stop_sequences",
    "thinking",
    "system",
)


class OpenAIMessage(BaseModel):
    """One turn of an OpenAI conversation.

    Attributes:
        role: ``system``, ``user``, ``assistant``, or ``tool``.
        content: The turn's text. May be null on an assistant turn that only
            carries tool calls.
        name: Optional participant name.
        tool_call_id: Present on ``tool`` turns; unused here.
    """

    model_config = ConfigDict(extra="allow")

    role: str = Field(..., description="system | user | assistant | tool")
    content: str | None = Field(default=None, description="The turn's text content.")
    name: str | None = Field(default=None, description="Optional participant name.")
    tool_call_id: str | None = Field(default=None, description="Tool result correlation id.")


class OpenAIFunctionDefinition(BaseModel):
    """The function half of an OpenAI tool definition.

    Attributes:
        name: The function name. Echoed back in the tool call.
        description: Optional human-readable summary.
        parameters: The JSON Schema describing the decisions to make.
    """

    model_config = ConfigDict(extra="allow")

    name: str = Field(..., description="Function name.")
    description: str | None = Field(default=None, description="What the function does.")
    parameters: dict[str, Any] = Field(
        default_factory=dict, description="JSON Schema for the arguments."
    )


class OpenAITool(BaseModel):
    """An OpenAI tool definition.

    Attributes:
        type: Always ``"function"``.
        function: The function definition.
    """

    model_config = ConfigDict(extra="allow")

    type: str = Field(default="function", description="Always 'function'.")
    function: OpenAIFunctionDefinition = Field(..., description="The function definition.")


class OpenAIChatRequest(BaseModel):
    """The subset of ``POST /v1/chat/completions`` this service acts on.

    Attributes:
        model: The model identifier.
        messages: The conversation.
        tools: Available tools. The model is selected from these.
        tool_choice: Which tool to use. Required in practice -- see the route.
        stream: Rejected when true; there is nothing to stream incrementally.
        n: Rejected when greater than 1; one forward pass yields one answer set.
    """

    model_config = ConfigDict(extra="allow")

    model: str = Field(default="english", description="Model identifier.")
    messages: list[OpenAIMessage] = Field(
        default_factory=list, description="The conversation so far."
    )
    tools: list[OpenAITool] | None = Field(default=None, description="Available tools.")
    tool_choice: Any = Field(default=None, description="Which tool to call.")
    stream: bool = Field(default=False, description="Rejected when true.")
    n: int = Field(default=1, description="Rejected when greater than 1.")


class AnthropicMessage(BaseModel):
    """One turn of an Anthropic conversation.

    Attributes:
        role: ``user`` or ``assistant``.
        content: Either a string or a list of content blocks.
    """

    model_config = ConfigDict(extra="allow")

    role: str = Field(..., description="user | assistant")
    content: Any = Field(default=None, description="A string or a list of content blocks.")


class AnthropicMessagesRequest(BaseModel):
    """The subset of ``POST /v1/messages`` this service acts on.

    Attributes:
        model: The model identifier.
        max_tokens: Required by the Anthropic API. Accepted and ignored -- there
            is no generation to bound.
        messages: The conversation.
        tools: Available tools, each carrying an ``input_schema``.
        tool_choice: Which tool to use.
        stream: Rejected when true.
    """

    model_config = ConfigDict(extra="allow")

    model: str = Field(default="english", description="Model identifier.")
    max_tokens: int | None = Field(
        default=None, description="Required by the SDK; ignored, as nothing is generated."
    )
    messages: list[AnthropicMessage] = Field(
        default_factory=list, description="The conversation so far."
    )
    tools: list[dict[str, Any]] | None = Field(default=None, description="Available tools.")
    tool_choice: Any = Field(default=None, description="Which tool to call.")
    stream: bool = Field(default=False, description="Rejected when true.")
