"""Architecture fitness functions.

These tests enforce the dependency rule mechanically. Without them, "the domain
must not import FastAPI" is a convention that erodes the first time someone
needs a quick fix; with them, the build fails.

They work by parsing each module's imports with :mod:`ast` and asserting on the
resulting graph -- no imports are executed, so a violation is reported rather
than a side effect.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

import pytest

SRC: Final[Path] = Path(__file__).resolve().parents[2] / "src" / "laya_service"

#: Third-party packages the innermost layer must never touch.
FRAMEWORK_MODULES: Final[frozenset[str]] = frozenset(
    {
        "fastapi",
        "starlette",
        "pydantic",
        "pydantic_settings",
        "uvicorn",
        "laya",
        "torch",
        "transformers",
        "numpy",
        "huggingface_hub",
        "requests",
        "httpx",
    }
)


def iter_modules(layer: str) -> list[Path]:
    """List the Python modules in a layer.

    Args:
        layer: Layer directory name, e.g. ``"domain"``.

    Returns:
        Every ``.py`` file under that layer, excluding empty ``__init__.py``s.
    """
    root = SRC / layer
    return [path for path in sorted(root.rglob("*.py")) if path.stat().st_size > 0]


def imported_top_level_names(path: Path) -> set[str]:
    """Return the top-level names imported by a module.

    Args:
        path: The module to parse.

    Returns:
        Top-level module names, e.g. ``{"fastapi", "laya_service"}``. Relative
        imports are reported as ``"laya_service"`` because that is what they
        resolve to.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                # A relative import always stays inside the package.
                names.add("laya_service")
            elif node.module:
                names.add(node.module.split(".")[0])
    return names


def laya_service_layers_imported(path: Path) -> set[str]:
    """Return which sibling layers a module imports from.

    Args:
        path: The module to parse.

    Returns:
        Layer names such as ``{"domain", "application"}``.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    layers: set[str] = set()
    for node in ast.walk(tree):
        module: str | None = None
        if isinstance(node, ast.ImportFrom) and node.module:
            module = node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("laya_service."):
                    parts = alias.name.split(".")
                    if len(parts) > 1:
                        layers.add(parts[1])
            continue
        if module and module.startswith("laya_service."):
            parts = module.split(".")
            if len(parts) > 1:
                layers.add(parts[1])
    return layers


class TestDomainPurity:
    """The domain is the innermost layer and must stay dependency-free."""

    def test_domain_modules_exist(self) -> None:
        """The layer is actually populated, so the checks below mean something."""
        assert iter_modules("domain")

    @pytest.mark.parametrize("path", iter_modules("domain"), ids=lambda p: p.name)
    def test_domain_imports_no_frameworks(self, path: Path) -> None:
        """No third-party import may appear anywhere in the domain."""
        violations = imported_top_level_names(path) & FRAMEWORK_MODULES
        assert not violations, f"{path.relative_to(SRC)} imports {sorted(violations)}"

    @pytest.mark.parametrize("path", iter_modules("domain"), ids=lambda p: p.name)
    def test_domain_imports_no_outer_layer(self, path: Path) -> None:
        """The domain must not depend on application, infrastructure, or interfaces."""
        forbidden = laya_service_layers_imported(path) & {
            "application",
            "infrastructure",
            "interfaces",
        }
        assert not forbidden, f"{path.relative_to(SRC)} imports {sorted(forbidden)}"


class TestApplicationLayer:
    """The application layer may see the domain and nothing else."""

    def test_application_modules_exist(self) -> None:
        """The layer is populated."""
        assert iter_modules("application")

    @pytest.mark.parametrize("path", iter_modules("application"), ids=lambda p: p.name)
    def test_application_imports_no_frameworks(self, path: Path) -> None:
        """Use cases and DTOs must not import FastAPI, Pydantic, or Laya."""
        violations = imported_top_level_names(path) & FRAMEWORK_MODULES
        assert not violations, f"{path.relative_to(SRC)} imports {sorted(violations)}"

    @pytest.mark.parametrize("path", iter_modules("application"), ids=lambda p: p.name)
    def test_application_imports_no_outer_layer(self, path: Path) -> None:
        """The application layer must not depend on infrastructure or interfaces."""
        forbidden = laya_service_layers_imported(path) & {"infrastructure", "interfaces"}
        assert not forbidden, f"{path.relative_to(SRC)} imports {sorted(forbidden)}"


class TestLayaIsolation:
    """Only the adapter may touch Laya."""

    def test_only_the_adapter_imports_laya(self) -> None:
        """``laya`` is confined to infrastructure/model/laya_adapter.py."""
        offenders = [
            path.relative_to(SRC)
            for path in SRC.rglob("*.py")
            if "laya" in imported_top_level_names(path) and path.name != "laya_adapter.py"
        ]
        assert not offenders, f"laya is imported outside the adapter: {offenders}"

    def test_only_infrastructure_imports_torch(self) -> None:
        """Heavy ML dependencies stay out of every other layer."""
        offenders = [
            path.relative_to(SRC)
            for path in SRC.rglob("*.py")
            if "torch" in imported_top_level_names(path)
        ]
        assert not offenders, f"torch is imported in {offenders}"


class TestLayerPopulation:
    """Every declared layer exists and holds code."""

    @pytest.mark.parametrize("layer", ["domain", "application", "infrastructure", "interfaces"])
    def test_layer_is_populated(self, layer: str) -> None:
        """Each layer has at least one real module."""
        assert iter_modules(layer), f"layer {layer!r} has no modules"
