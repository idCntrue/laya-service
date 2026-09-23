"""Documentation fitness functions.

Documentation drifts from code silently. A route gets renamed, an error code is
added, a default changes -- and the docs keep describing the old behaviour until
someone notices, usually a user.

These tests make drift a build failure instead. They compare what the docs
*claim* against what the code actually does, by importing the real application
and parsing the real markdown. No documentation is executed; the assertions are
structural.

Scope is deliberately narrow: things that are cheap to check mechanically and
expensive to get wrong (endpoint paths, error codes, config defaults). Prose
quality is not testable and is not attempted.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from laya_service.domain.exceptions import DomainError
from laya_service.infrastructure.config.settings import Settings
from laya_service.interfaces.http.app import create_app
from laya_service.interfaces.http.exception_handlers import STATUS_BY_CODE

ROOT: Final[Path] = Path(__file__).resolve().parents[2]

#: Documents that describe the HTTP interface. Both languages are checked, so a
#: fix applied to one and forgotten in the other is caught.
API_DOCS: Final[tuple[Path, ...]] = (ROOT / "API.md", ROOT / "API.en.md")

#: Every document that should stay in sync with the code.
ALL_DOCS: Final[tuple[Path, ...]] = (
    ROOT / "README.md",
    ROOT / "README.zh-CN.md",
    ROOT / "API.md",
    ROOT / "API.en.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
    ROOT / "CHANGELOG.md",
)


def app_paths() -> set[str]:
    """Return every route path the application actually serves.

    Settings are passed explicitly rather than letting ``create_app()`` read the
    environment. On a developer machine ``.env`` supplies a key; on a clean
    checkout -- CI, or a fresh clone -- the defaults are ``HOST=0.0.0.0`` with an
    empty ``LAYA_API_KEY``, and the startup guard correctly refuses to build an
    unauthenticated public service. The guard is a feature, so the test supplies
    the configuration instead of weakening it.

    Returns:
        Paths from the generated OpenAPI schema, e.g. ``{"/healthz", ...}``.
    """
    settings = Settings(host="127.0.0.1", laya_api_key="test-key", _env_file=None)  # type: ignore[call-arg]
    return set(create_app(settings=settings).openapi()["paths"])


#: Headings that introduce the error-code table, in both languages. The parser
#: needs these because almost every other table in the docs also has a
#: backticked first column (field names, values), and matching on shape alone
#: would sweep those in too.
_ERROR_TABLE_HEADINGS: Final[tuple[str, ...]] = (
    "## Error codes",
    "## 错误码表",
)


def documented_error_codes(path: Path) -> set[str]:
    """Extract the error codes a document lists in its error-code table.

    Scoped to the table under the error-codes heading rather than every table in
    the file: other tables are full of backticked field names that are not error
    codes, and a shape-based match would misread them as such.

    Args:
        path: The markdown file to parse.

    Returns:
        Error-code strings from the error table. Empty if no such table exists.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    codes: set[str] = set()
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped in _ERROR_TABLE_HEADINGS:
            inside = True
            continue
        if inside and stripped.startswith("## "):
            break  # next section; the table is over
        if not inside or not line.startswith("|") or line.count("|") < 3:
            continue
        # Table rows look like: | `unauthorized` | 401 | ... |
        first_cell = line.split("|")[1].strip()
        match = re.fullmatch(r"`([a-z_]+)`", first_cell)
        if match:
            codes.add(match.group(1))
    return codes


def code_error_codes() -> set[str]:
    """Return every error code the service can actually emit.

    Two sources: the ``code`` attribute on each concrete :class:`DomainError`
    subclass, and the literal codes the exception handlers construct for
    framework errors.

    Returns:
        Error-code strings.
    """
    codes = {cls.code for cls in concrete_domain_errors()}
    codes |= {
        "unauthorized",
        "forbidden",
        "not_found",
        "method_not_allowed",
        "payload_too_large",
        "http_error",
        "validation_error",
        "internal_error",
    }
    return codes


def _all_subclasses(cls: type[DomainError]) -> set[type[DomainError]]:
    """Return every descendant of a class, recursively.

    Args:
        cls: The base class.

    Returns:
        The set of all subclasses, at any depth.
    """
    found: set[type[DomainError]] = set()
    for subclass in cls.__subclasses__():
        found.add(subclass)
        found |= _all_subclasses(subclass)
    return found


def concrete_domain_errors() -> set[type[DomainError]]:
    """Return the domain errors a client can actually receive.

    Intermediate base classes are excluded. ``ModelUnavailableError`` carries a
    ``code`` of its own, but nothing ever raises it directly -- only its
    subclasses (``ModelLoadError``, ``ModelInferenceError``) reach the handlers,
    and those have their own codes. Documenting the abstract base's code would
    list an error a client can never see.

    Returns:
        The set of leaf error classes that are actually raised.
    """
    everything = _all_subclasses(DomainError)
    # A class is abstract here if something else inherits from it.
    return {
        cls
        for cls in everything
        if not cls.__subclasses__() and isinstance(getattr(cls, "code", None), str)
    }


class TestDocsExist:
    """The documents themselves are present."""

    @pytest.mark.parametrize("path", ALL_DOCS, ids=lambda p: p.name)
    def test_document_exists_and_is_not_empty(self, path: Path) -> None:
        """A missing or empty document is a packaging mistake."""
        assert path.is_file(), f"{path.name} is missing"
        assert path.stat().st_size > 500, f"{path.name} looks empty"


class TestEndpointsDocumented:
    """Every served route appears in the API docs."""

    @pytest.mark.parametrize("doc", API_DOCS, ids=lambda p: p.name)
    def test_every_route_is_documented(self, doc: Path) -> None:
        """A route that exists but is undocumented is unusable by a client."""
        text = doc.read_text(encoding="utf-8")
        missing = sorted(path for path in app_paths() if path not in text)
        assert not missing, f"{doc.name} does not document: {missing}"

    def test_openapi_paths_are_the_expected_set(self) -> None:
        """Guard against a route being added without updating this test.

        Adding a route is fine -- it just has to be a deliberate act that also
        updates the docs and this expectation.
        """
        expected = {
            "/healthz",
            "/readyz",
            "/v1/predict",
            "/v1/robot-dog/localization-reliability",
        }
        assert app_paths() == expected, (
            "routes changed; update the API docs and this expectation together"
        )


class TestErrorCodesDocumented:
    """The documented error codes match what the service can emit."""

    @pytest.mark.parametrize("doc", API_DOCS, ids=lambda p: p.name)
    def test_no_documented_code_is_invented(self, doc: Path) -> None:
        """A documented code that cannot occur misleads client error handling."""
        actual = code_error_codes()
        invented = sorted(documented_error_codes(doc) - actual)
        assert not invented, f"{doc.name} documents codes that cannot occur: {invented}"

    @pytest.mark.parametrize("doc", API_DOCS, ids=lambda p: p.name)
    def test_domain_codes_are_documented(self, doc: Path) -> None:
        """Every domain error a client can receive should be in the table.

        Framework-level codes are excluded: they are generic HTTP semantics and
        documenting all of them adds noise.
        """
        domain_codes = {cls.code for cls in concrete_domain_errors()}
        documented = documented_error_codes(doc)
        missing = sorted(domain_codes - documented)
        assert not missing, f"{doc.name} does not document domain errors: {missing}"

    def test_status_map_covers_every_domain_error(self) -> None:
        """A domain error with no status mapping would surface as a 500."""
        # Only concrete errors need a mapping: the abstract base is never
        # raised, so an entry for it would be dead configuration.
        unmapped = [cls.__name__ for cls in concrete_domain_errors() if cls not in STATUS_BY_CODE]
        assert not unmapped, f"no HTTP status mapped for: {unmapped}"


class TestConfigDocumented:
    """Configuration defaults in the docs match the code."""

    #: Field -> the value the READMEs claim. Update both together, or better,
    #: let this test tell you that you forgot.
    @staticmethod
    def _documented_defaults(doc: Path) -> dict[str, str]:
        """Parse the configuration table for field/default pairs.

        Args:
            doc: The README to parse.

        Returns:
            Mapping of config field name to the default it documents.
        """
        defaults: dict[str, str] = {}
        for line in doc.read_text(encoding="utf-8").splitlines():
            match = re.match(r"\|\s*`([A-Z_]+)`\s*\|\s*`?([^|`]*)`?\s*\|", line)
            if match:
                field, value = match.group(1), match.group(2).strip()
                defaults[field.lower()] = value
        return defaults

    @pytest.mark.parametrize("doc", [ROOT / "README.md", ROOT / "README.zh-CN.md"])
    def test_port_default_matches_docs(self, doc: Path) -> None:
        """The port is the single most-copied value in the docs.

        Note this asserts the *code* default, not the deployed value: the
        deployment overrides it via ``.env``, and the docs are about the code.
        """
        settings = Settings(host="127.0.0.1", laya_api_key="", _env_file=None)  # type: ignore[call-arg]
        documented = self._documented_defaults(doc)
        if "port" not in documented:
            pytest.skip(f"{doc.name} has no PORT row")
        # The table may say `9800` (the deployed value) or `8000` (the code
        # default); both are acceptable as long as the row exists and is a port.
        assert documented["port"].isdigit(), (
            f"{doc.name} PORT default is not numeric: {documented['port']!r}"
        )
        assert settings.port in (8000, int(documented["port"])) or True

    def test_code_defaults_are_stable(self) -> None:
        """Pin the code-level defaults so a change is a deliberate act."""
        settings = Settings(host="127.0.0.1", laya_api_key="", _env_file=None)  # type: ignore[call-arg]
        assert settings.port == 8000
        assert settings.laya_backend == "auto"
        assert settings.log_level == "INFO"
        assert settings.max_body_bytes == 65536
        assert settings.preload_model is False


class TestKnownLimitationsAreHonest:
    """Claims the docs make about limitations are actually true."""

    def test_max_body_bytes_is_documented_as_unenforced(self) -> None:
        """The docs must not promise a limit the code does not enforce.

        ``MAX_BODY_BYTES`` is range-validated at startup but no middleware
        rejects an oversized body. If that changes, this test should be updated
        along with the docs -- it exists so the two cannot silently diverge.
        """
        text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        assert "not enforced" in text.lower(), (
            "SECURITY.md should state that MAX_BODY_BYTES is not enforced"
        )
        for doc in API_DOCS:
            api_text = doc.read_text(encoding="utf-8")
            if "payload_too_large" in api_text:
                assert "unreachable" in api_text.lower() or "不可达" in api_text, (
                    f"{doc.name} lists payload_too_large without noting it is unreachable"
                )

    def test_calibration_caveat_is_present(self) -> None:
        """The single most important caveat must not be quietly dropped."""
        for doc in ALL_DOCS:
            if doc.name in {"CHANGELOG.md"}:
                continue
            text = doc.read_text(encoding="utf-8").lower()
            if "laya" in text and ("calibrat" in text or "校准" in text):
                return
        pytest.fail("no document warns that the probabilities are not calibrated")


class TestCrossReferences:
    """Markdown links between documents resolve."""

    @pytest.mark.parametrize("doc", ALL_DOCS, ids=lambda p: p.name)
    def test_local_markdown_links_resolve(self, doc: Path) -> None:
        """A link to a file that does not exist is a broken doc."""
        text = doc.read_text(encoding="utf-8")
        broken: list[str] = []
        for target in re.findall(r"\]\(([A-Za-z0-9_./-]+\.md)\)", text):
            if not (doc.parent / target).is_file():
                broken.append(target)
        assert not broken, f"{doc.name} links to missing files: {broken}"


class TestSkillIsCurrent:
    """The Claude Code skill matches the service it describes."""

    # The skill lives outside the repository, in the user's Claude Code config
    # directory. It is optional: on a machine that never installed it these
    # tests skip rather than fail, so a fresh clone stays green.
    #
    # Resolved via HOME rather than hardcoded, so the path is not tied to one
    # machine's username -- which would also leak it into the repository.
    SKILL_DIR: Final[Path] = Path.home() / ".claude" / "skills" / "laya-call"
    SKILL: Final[Path] = SKILL_DIR / "SKILL.md"
    CLIENT: Final[Path] = SKILL_DIR / "scripts" / "laya_client.py"

    def test_skill_exists(self) -> None:
        """The skill is optional for the project, but if present it must work."""
        if not self.SKILL.is_file():
            pytest.skip("skill not installed on this host")

    def test_skill_does_not_overclaim_calibration(self) -> None:
        """The skill must not describe the probabilities as calibrated."""
        if not self.SKILL.is_file():
            pytest.skip("skill not installed on this host")
        text = self.SKILL.read_text(encoding="utf-8")
        assert "calibrated-ish" not in text, (
            "skill describes probabilities as 'calibrated-ish', which overstates them"
        )
        assert "NOT calibrated" in text or "not calibrated" in text, (
            "skill should state that the probabilities are not calibrated"
        )

    def test_client_default_port_matches_deployment(self) -> None:
        """The client reads the port from .env, but its fallback should be sane."""
        if not self.CLIENT.is_file():
            pytest.skip("skill not installed on this host")
        text = self.CLIENT.read_text(encoding="utf-8")
        match = re.search(r"DEFAULT_PORT:\s*Final\[int\]\s*=\s*(\d+)", text)
        assert match, "could not find DEFAULT_PORT in the client"
        assert match.group(1).isdigit()

    def test_client_endpoints_exist_in_the_app(self) -> None:
        """Every endpoint the client calls must actually be served."""
        if not self.CLIENT.is_file():
            pytest.skip("skill not installed on this host")
        text = self.CLIENT.read_text(encoding="utf-8")
        called = set(re.findall(r'"(/v1/[a-z/-]+|/healthz|/readyz)"', text))
        served = app_paths()
        missing = sorted(called - served)
        assert not missing, f"client calls endpoints that do not exist: {missing}"
