"""Process entry point.

Deliberately the thinnest module in the package. It resolves configuration and
hands control to Uvicorn; all wiring lives in
:func:`laya_service.interfaces.http.app.create_app`.

The factory form is used (``factory=True``) rather than importing an ``app``
object, so that the application is constructed *inside* the worker process.
That matters when the model is preloaded: an app object built at import time in
a parent process would not be the one serving requests.

Usage::

    python -m laya_service.main
    laya-service                # console script from pyproject.toml
"""

from __future__ import annotations

import sys

import uvicorn

from laya_service.infrastructure.config.settings import get_settings
from laya_service.logging_config import configure_logging, get_logger

__all__ = ["main"]

_logger = get_logger(__name__, component="main")


def main() -> int:
    """Run the HTTP server.

    Returns:
        A process exit code: ``0`` on clean shutdown, ``1`` on a configuration
        error. Configuration errors are reported without a traceback because
        they are operator mistakes, not bugs.
    """
    try:
        settings = get_settings()
    except ValueError as exc:
        # Configuration problems are the operator's to fix, and a stack trace
        # only obscures the message. Print it plainly and exit non-zero.
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1

    configure_logging(settings.log_level)
    _logger.info(
        "starting uvicorn",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
    )

    uvicorn.run(
        "laya_service.interfaces.http.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        # Trust X-Forwarded-* only from a proxy we control. Default is safe
        # (loopback only) and can be widened via the env var when deployed
        # behind a known ingress.
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        # Give in-flight inference a chance to finish rather than dropping the
        # connection on SIGTERM.
        timeout_graceful_shutdown=30,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
