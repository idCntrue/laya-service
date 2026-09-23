"""Laya Service -- decision-model inference behind a Clean Architecture boundary.

The package is organised in four concentric layers, with dependencies pointing
strictly inward::

    interfaces      ->  application  ->  domain
    infrastructure  ->  application  ->  domain

``domain`` imports nothing but the standard library. ``application`` imports
only ``domain``. ``infrastructure`` and ``interfaces`` are adapters: the former
drives the outside world (models, configuration), the latter is driven by it
(HTTP). Neither is imported by the layers beneath them.
"""

from __future__ import annotations

__all__ = ["__version__"]

#: Service version, surfaced in ``/healthz`` and the OpenAPI document.
__version__ = "0.1.0"
