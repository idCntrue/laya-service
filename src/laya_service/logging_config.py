"""Structured JSON logging.

Every log record is emitted as a single JSON object on one line, so it can be
shipped straight into ELK, Loki, or CloudWatch Logs without a parsing rule.
The fields ``request_id``, ``trace_id``, ``latency_ms``, ``method``, ``path``
and ``status`` are the contract the access-log middleware and the request-id
middleware uphold.

Secrets never reach a log line: the redaction filter below scrubs anything that
looks like a bearer token or an API-key assignment as a defence-in-depth measure
on top of simply not logging them.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import TYPE_CHECKING, Any, Final

__all__ = [
    "JsonFormatter",
    "SecretRedactingFilter",
    "StructuredLogger",
    "bind_context",
    "configure_logging",
    "get_logger",
    "log_extra",
]

#: Attributes present on every ``LogRecord`` by default. Anything outside this
#: set was added by us via ``extra=`` and should be serialised into the payload.
_RESERVED_ATTRS: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

#: Patterns matching credentials that must never be written to a log sink.
_SECRET_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[:=]\s*\S+"),
)


class SecretRedactingFilter(logging.Filter):
    """Scrub credential-shaped substrings from log messages.

    This is defence in depth, not the primary control -- the primary control is
    not passing secrets to the logger in the first place. It exists so that an
    accidental ``logger.info(f"headers={headers}")`` cannot leak a token.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact secrets in ``record.msg`` and any string ``extra`` values.

        Args:
            record: The record about to be emitted.

        Returns:
            Always ``True``; this filter mutates rather than drops.
        """
        if isinstance(record.msg, str):
            record.msg = self._scrub(record.msg)
        for key, value in list(record.__dict__.items()):
            if key not in _RESERVED_ATTRS and isinstance(value, str):
                record.__dict__[key] = self._scrub(value)
        return True

    @staticmethod
    def _scrub(text: str) -> str:
        """Replace credential-shaped substrings with a placeholder.

        Args:
            text: The string to scrub.

        Returns:
            The scrubbed string.
        """
        scrubbed = text
        for pattern in _SECRET_PATTERNS:
            scrubbed = pattern.sub(lambda m: f"{m.group(1)} ***redacted***", scrubbed)
        return scrubbed


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialise a record to JSON.

        Args:
            record: The record to format.

        Returns:
            A JSON object with ``timestamp``, ``level``, ``logger``, ``message``,
            and every ``extra`` field supplied by the caller. Exceptions are
            rendered into an ``exception`` key.
        """
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger.

    Idempotent: repeated calls replace the handler rather than stacking new ones,
    which matters under Uvicorn's reloader and in tests.

    Args:
        level: Minimum level name, e.g. ``"INFO"``.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(SecretRedactingFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Uvicorn installs its own handlers; route them through ours so that access
    # lines are JSON too, and do not double-log via propagation.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True


#: Keyword arguments ``logging`` itself understands on a log call. Anything
#: else a caller passes is structured context, not a logging directive.
_LOGGING_KWARGS: Final[frozenset[str]] = frozenset(
    {"exc_info", "stack_info", "stacklevel", "extra"}
)


if TYPE_CHECKING:
    # ``LoggerAdapter`` is generic in its stubs but NOT subscriptable at runtime
    # on Python 3.10 -- `class X(logging.LoggerAdapter[logging.Logger])` raises
    # TypeError: 'type' object is not subscriptable. Writing the parameter
    # inline would break the import; omitting it entirely makes `mypy --strict`
    # complain about a missing type argument, and a bare `# type: ignore` for
    # that is itself reported as unused under some Python targets.
    #
    # Declaring a subscripted alias under TYPE_CHECKING gives mypy the generic
    # form to check against while the runtime sees the plain, subscriptable
    # class. This keeps one definition working under 3.10, 3.11 and 3.12.
    _LoggerAdapter = logging.LoggerAdapter[logging.Logger]
else:  # pragma: no cover - runtime only
    _LoggerAdapter = logging.LoggerAdapter


class StructuredLogger(_LoggerAdapter):
    """A :class:`logging.LoggerAdapter` that accepts arbitrary context fields.

    The stdlib's ``Logger`` only tolerates ``exc_info``, ``stack_info``,
    ``stacklevel`` and ``extra`` as keyword arguments; passing anything else --
    the natural way to write ``logger.info("loaded", model=name)`` -- raises
    ``TypeError`` at the call site. This adapter intercepts those extra keywords
    and folds them into ``extra``, so structured logging reads the way it should
    and the JSON formatter picks the fields up automatically.
    """

    def process(
        self, msg: Any, kwargs: MutableMapping[str, Any]
    ) -> tuple[Any, MutableMapping[str, Any]]:
        """Move unrecognised keyword arguments into the ``extra`` payload.

        Args:
            msg: The log message.
            kwargs: The keyword arguments supplied at the call site.

        Returns:
            The message unchanged, and a kwargs mapping containing only keys the
            stdlib logger accepts -- with everything else merged into ``extra``.
        """
        structured = {key: value for key, value in kwargs.items() if key not in _LOGGING_KWARGS}
        rest = {key: value for key, value in kwargs.items() if key in _LOGGING_KWARGS}

        merged: dict[str, Any] = dict(self.extra or {})
        merged.update(kwargs.get("extra") or {})
        # A structured field whose name collides with a LogRecord attribute
        # cannot be placed in `extra` -- `logging` raises
        # ``KeyError: Attempt to overwrite 'name' in LogRecord`` when the record
        # is built, which turns a log call into a 500. Names like ``name``,
        # ``module``, ``args`` and ``message`` are reserved, and ``name`` in
        # particular is the obvious thing to call a field. Such keys are
        # prefixed rather than dropped, so the value still reaches the sink
        # under a predictable name.
        for key, value in structured.items():
            merged[f"x_{key}" if key in _RESERVED_ATTRS else key] = value
        rest["extra"] = merged
        return msg, rest

    # The stdlib's LoggerAdapter annotates these methods with a fixed set of
    # keyword arguments, so a call like `log.info("x", model=name)` is a type
    # error even though `process` above handles it at runtime. Re-declaring them
    # with `**kwargs: Any` keeps static analysis aligned with the actual
    # behaviour, which is the whole point of this subclass.

    def debug(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        """Log at DEBUG, accepting arbitrary structured fields as keywords."""
        self.log(logging.DEBUG, msg, *args, **kwargs)

    def info(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        """Log at INFO, accepting arbitrary structured fields as keywords."""
        self.log(logging.INFO, msg, *args, **kwargs)

    def warning(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        """Log at WARNING, accepting arbitrary structured fields as keywords."""
        self.log(logging.WARNING, msg, *args, **kwargs)

    def error(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        """Log at ERROR, accepting arbitrary structured fields as keywords."""
        self.log(logging.ERROR, msg, *args, **kwargs)

    def critical(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        """Log at CRITICAL, accepting arbitrary structured fields as keywords."""
        self.log(logging.CRITICAL, msg, *args, **kwargs)

    def exception(self, msg: Any, *args: Any, **kwargs: Any) -> None:
        """Log at ERROR with exception info, accepting structured fields."""
        kwargs.setdefault("exc_info", True)
        self.log(logging.ERROR, msg, *args, **kwargs)


def get_logger(name: str, **context: Any) -> StructuredLogger:
    """Return a logger pre-bound with structured context.

    Args:
        name: Logger name, conventionally ``__name__``.
        **context: Extra fields merged into every record, e.g. ``component="model"``.

    Returns:
        A :class:`StructuredLogger` that injects ``context`` into each call and
        accepts further ad-hoc fields as keyword arguments.
    """
    return StructuredLogger(logging.getLogger(name), context)


def bind_context(logger: StructuredLogger, **context: Any) -> StructuredLogger:
    """Return a copy of ``logger`` with additional bound context.

    Args:
        logger: An adapter produced by :func:`get_logger`.
        **context: Fields to merge into the existing context.

    Returns:
        A new adapter; the original is left untouched.
    """
    merged: MutableMapping[str, Any] = dict(logger.extra or {})
    merged.update(context)
    return StructuredLogger(logger.logger, merged)


def log_extra(**fields: Any) -> Mapping[str, Any]:
    """Build an ``extra=`` payload that the JSON formatter will flatten.

    Prefer passing fields directly -- :class:`StructuredLogger` accepts them as
    keyword arguments. This helper remains for code that calls a plain
    :class:`logging.Logger`, where a colliding key would otherwise raise.

    Args:
        **fields: Desired structured fields.

    Returns:
        A mapping safe to pass as ``logger.info(..., extra=...)``.
    """
    safe: dict[str, Any] = {}
    for key, value in fields.items():
        safe[f"x_{key}" if key in _RESERVED_ATTRS else key] = value
    return {"extra": safe}
