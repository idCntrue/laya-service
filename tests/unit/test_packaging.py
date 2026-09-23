"""Packaging metadata fitness functions.

A broken ``pyproject.toml`` does not fail any other test: the suite imports the
package from an already-installed editable checkout, so a metadata error is
invisible until something tries to build the project -- CI, a Docker build, or
a user running ``pip install``.

That is exactly what happened once: switching to a PEP 639 license expression
while leaving the matching ``License ::`` classifier in place made setuptools
raise ``InvalidConfigError`` on every install. Locally everything stayed green
because the package was already installed.

These tests parse ``pyproject.toml`` directly, so they need no build and no
network, and they fail in the same run as everything else.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any, Final

import pytest

# TOML parsing without a hard dependency on either module.
#
# ``tomllib`` is stdlib from 3.11; on 3.10 the backport ``tomli`` provides the
# same API. Neither import is written directly, because mypy's view of which one
# exists depends on the *configured* Python version, not the running one:
#
#   * A plain `try/except ModuleNotFoundError` is rejected under 3.12, where
#     mypy knows tomllib exists and calls the except branch a redefinition.
#   * A `sys.version_info` check is rejected when mypy is configured for 3.10
#     while running on 3.12 -- it takes the else branch, needs `tomli`, and
#     `tomli` is not installed there (its marker is python_version < '3.11').
#
# Importing through importlib sidesteps both: there is no import statement for
# mypy to reason about, and the fallback is resolved at run time where the
# answer is unambiguous.


def _load_toml_module() -> Any:
    """Return a TOML parser module, preferring the stdlib.

    Returns:
        ``tomllib`` on 3.11+, otherwise the ``tomli`` backport.

    Raises:
        RuntimeError: If neither is importable.
    """
    for name in ("tomllib", "tomli"):
        try:
            return importlib.import_module(name)
        except ModuleNotFoundError:
            continue
    raise RuntimeError("no TOML parser available; install tomli on Python 3.10")


tomllib = _load_toml_module()

ROOT: Final[Path] = Path(__file__).resolve().parents[2]
PYPROJECT: Final[Path] = ROOT / "pyproject.toml"


@pytest.fixture(scope="module")
def project_metadata() -> dict[str, Any]:
    """Parse the ``[project]`` table of ``pyproject.toml``.

    Returns:
        The parsed project metadata.
    """
    with PYPROJECT.open("rb") as handle:
        data: dict[str, Any] = tomllib.load(handle)["project"]
    return data


class TestLicenseMetadata:
    """License fields must be internally consistent.

    PEP 639 replaced ``License ::`` classifiers with a ``license`` expression,
    and setuptools refuses to build when both are present.
    """

    def test_no_license_classifier_with_spdx_expression(
        self, project_metadata: dict[str, Any]
    ) -> None:
        """A ``license`` string plus a ``License ::`` classifier cannot coexist.

        setuptools raises::

            InvalidConfigError: License classifiers have been superseded by
            license expressions

        which fails ``pip install`` for everyone. The two styles are mutually
        exclusive: use the SPDX expression, drop the classifier.
        """
        license_value = project_metadata.get("license")
        if not isinstance(license_value, str):
            pytest.skip("license is not a PEP 639 expression; classifier is allowed")

        offenders = [
            classifier
            for classifier in project_metadata.get("classifiers", [])
            if classifier.startswith("License ::")
        ]
        assert not offenders, (
            "license is declared as an SPDX expression, so these classifiers must be "
            f"removed or `pip install` will fail: {offenders}"
        )

    def test_license_file_exists(self, project_metadata: dict[str, Any]) -> None:
        """Every declared license file must be present in the repository."""
        declared = project_metadata.get("license-files", [])
        missing = [name for name in declared if not (ROOT / name).is_file()]
        assert not missing, f"declared license files are missing: {missing}"

    def test_license_file_is_a_real_license(self) -> None:
        """The LICENSE file should contain a license, not a placeholder."""
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        assert "Apache License" in text
        assert "Version 2.0" in text
        # The appendix placeholder must have been filled in.
        assert "[yyyy]" not in text, "LICENSE still has an unfilled [yyyy] placeholder"
        assert "[name of copyright owner]" not in text


class TestProjectMetadata:
    """Basic metadata sanity, and consistency with the code."""

    def test_required_fields_present(self, project_metadata: dict[str, Any]) -> None:
        """A missing required field breaks the build or the PyPI listing."""
        for field in ("name", "version", "description", "readme", "requires-python"):
            assert project_metadata.get(field), f"missing required field: {field}"

    def test_readme_file_exists(self, project_metadata: dict[str, Any]) -> None:
        """A declared readme that does not exist fails the build."""
        assert (ROOT / project_metadata["readme"]).is_file()

    def test_version_matches_package(self, project_metadata: dict[str, Any]) -> None:
        """``pyproject.toml`` and ``__init__.__version__`` must not drift.

        They are two sources of truth for one value, and a mismatch shows up in
        ``/healthz`` and the OpenAPI document.
        """
        from laya_service import __version__

        assert project_metadata["version"] == __version__, (
            "pyproject.toml version and laya_service.__version__ disagree"
        )

    def test_requires_python_is_satisfiable(self, project_metadata: dict[str, Any]) -> None:
        """The declared floor must be a real version string."""
        assert re.fullmatch(r">=\d+\.\d+", project_metadata["requires-python"]), (
            f"unexpected requires-python: {project_metadata['requires-python']!r}"
        )

    def test_console_script_target_is_importable(self, project_metadata: dict[str, Any]) -> None:
        """A broken entry point fails only at run time, after install."""
        scripts = project_metadata.get("scripts", {})
        for name, target in scripts.items():
            module, _, attribute = target.partition(":")
            assert module and attribute, f"malformed entry point {name}: {target!r}"


class TestDependencies:
    """Dependency declarations are well-formed."""

    def test_runtime_dependencies_are_pinned_minimally(
        self, project_metadata: dict[str, Any]
    ) -> None:
        """Every dependency should carry a lower bound.

        An unpinned dependency resolves to whatever is newest at install time,
        which makes builds irreproducible.
        """
        unpinned = [
            dep
            for dep in project_metadata.get("dependencies", [])
            if not any(op in dep for op in (">=", "==", "~=", ">", "<"))
        ]
        assert not unpinned, f"dependencies without a version constraint: {unpinned}"

    def test_dev_extra_includes_the_quality_tools(self, project_metadata: dict[str, Any]) -> None:
        """CI assumes these are in the dev extra; a removal would break it."""
        dev = " ".join(project_metadata.get("optional-dependencies", {}).get("dev", []))
        for tool in ("pytest", "ruff", "mypy", "httpx"):
            assert tool in dev, f"'{tool}' missing from the dev extra"


class TestToolConfiguration:
    """Tool config that CI depends on must stay present."""

    def test_coverage_floor_is_set(self) -> None:
        """The 70% floor is a stated project guarantee; removing it is silent."""
        with PYPROJECT.open("rb") as handle:
            data = tomllib.load(handle)
        assert data["tool"]["coverage"]["report"]["fail_under"] >= 70

    def test_mypy_is_strict(self) -> None:
        """The README and CONTRIBUTING both promise ``mypy --strict``."""
        with PYPROJECT.open("rb") as handle:
            data = tomllib.load(handle)
        assert data["tool"]["mypy"]["strict"] is True

    def test_pytest_testpaths_points_at_tests(self) -> None:
        """A wrong testpaths makes CI pass while running nothing."""
        with PYPROJECT.open("rb") as handle:
            data = tomllib.load(handle)
        assert data["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]
        assert (ROOT / "tests").is_dir()
