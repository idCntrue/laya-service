# Contributing

Thanks for considering a contribution. This document covers what you need to
get a change merged.

## Before you start

For anything beyond a small fix, **open an issue first**. This is a small
project with a specific architecture, and a short conversation up front is
cheaper than a large PR that has to be reworked.

Issues that are especially welcome:

- Bugs, with a reproduction
- Documentation that is wrong, unclear, or missing
- Deployment problems on a platform not covered in the README
- Security issues — **privately**, see [SECURITY.md](SECURITY.md)

## Development setup

```bash
git clone https://github.com/idCntrue/laya-service.git
cd laya-service
make install-dev
```

`make install-dev` creates `.venv`, installs CPU-only PyTorch, installs the
project with dev dependencies, and wires up the pre-commit hooks.

On a machine without `python3-venv`, the Makefile bootstraps pip via
`get-pip.py` — no `sudo` is required at any point.

### Why CPU-only PyTorch

`laya` depends on `torch>=2.0.0`. A plain `pip install torch` on a GPU-less
machine still downloads the CUDA build (~2.5 GB) plus a dozen `nvidia-*` wheels
that can never load. `make install-torch` installs `torch==2.9.1+cpu` (~176 MB)
first so the resolver never considers the CUDA build. Install in that order if
you do it by hand.

## Quality gates

Everything below must pass. CI runs the same commands.

```bash
make check        # lint + format-check + typecheck + test
```

Individually:

```bash
make lint          # ruff check
make format        # ruff check --fix && ruff format
make typecheck     # mypy --strict
make test          # pytest
make test-cov      # pytest --cov, fails under 70%
```

- **`mypy --strict` must pass.** No new `# type: ignore` without a comment
  explaining why the checker cannot see what you can.
- **Coverage floor is 70%**, currently 92%. A change that drops it
  significantly needs a reason.
- **Every public function and class needs a Google-style docstring.**

### If you change `pyproject.toml`, re-run the install

`make test` does **not** verify that the project can be installed. The suite
imports `laya_service` from the editable checkout that `make install-dev`
already created, so a metadata error is invisible until something rebuilds:

```bash
.venv/bin/pip install -e ".[dev]"    # after touching pyproject.toml
```

This is not hypothetical. Switching to a PEP 639 license expression while
leaving the matching `License ::` classifier in place made setuptools raise
`InvalidConfigError` on every build. All 308 tests passed locally; every CI job
failed. The package had been installed once and never rebuilt.

`tests/unit/test_packaging.py` now covers the invariants that only surface at
build time (license style consistency, declared files existing, the version
agreeing with `__init__`, the tool config CI depends on), so the common cases
fail in `make test`. It parses `pyproject.toml` directly and needs no build and
no network — but it cannot catch everything a real build would, so re-running
the install is still the reliable check.

## Architectural rules

These are enforced by tests, not by convention — `tests/unit/test_architecture.py`
parses imports with `ast` and fails the build on a violation.

| Layer | May import | Must never import |
|---|---|---|
| `domain` | stdlib only | anything else |
| `application` | `domain`, stdlib | `infrastructure`, `interfaces`, frameworks |
| `infrastructure` | `domain`, `application`, frameworks | `interfaces` |
| `interfaces` | everything inward | — |

**The rule that matters most: `domain` has zero third-party dependencies, and
`application` never imports a framework.** If you find yourself wanting
`import fastapi` in a use case, the design is telling you the logic belongs
somewhere else.

**Only `infrastructure/model/laya_adapter.py` may import `laya` or `torch`.**
Swapping the model should be a one-file change. If your change touches `laya`
anywhere else, it belongs behind the port.

**Business rules go in `application/use_cases/`.** Thresholds, fallback
behaviour, and recommendation mapping are policy — not something a route handler
or an adapter should decide.

## Testing

The test suite runs in about a second because `FakeDecisionModel` implements the
`DecisionModel` port. No weights, no network, no GPU.

- Unit tests for logic, using the fake
- Integration tests in `tests/integration/` exercise the real HTTP stack with
  only the model dependency overridden
- Mock `laya` at the `importlib` boundary in adapter tests

If your change is a bug fix, **add a test that fails without it**. Several of
the fixes in [CHANGELOG.md](CHANGELOG.md) were subtle enough that the test is
the only thing preventing a regression.

## Commits and pull requests

Commits: imperative mood, one logical change each. `Fix noul probability
inversion` rather than `fixed bug`.

A PR should include:

- What changed and why
- How you verified it (the command you ran, and what you saw)
- Any behaviour change a caller would notice

Keep the diff focused. Unrelated reformatting makes a change hard to review and
will be asked to be split out.

## Documentation

If you change the API, update **both** `API.md` (Chinese) and `API.en.md`
(English), plus the OpenAPI descriptions in `interfaces/http/schemas/`. The two
language versions drift easily; a change to one without the other is a bug.

Run any command you document. Several examples in these docs were corrected only
by actually executing them — for instance, `score` and `choice` responses carry
`legend` and `probabilities` fields that an earlier draft omitted.

## Working with Laya itself

A few things about the underlying library that are easy to get wrong. All of
these were discovered the hard way and are worth reading before touching the
adapter:

- The class is **`laya.Agent`**, not `laya.Laya`.
- Question types are **`noul`, `choice`, `score`** — not `mcq`/`multi`/`rng`.
- A `noul` answer is a **float probability of "true"**, and `confidence` is a
  **separate** field. A model can be 95% confident the answer is *false*
  (`noul=0.05, confidence=0.95`).
- `agent.predict` is an alias for `agent.system_one`.
- The response is an envelope: `{"model", "answers", "usage"}`.

When in doubt, introspect rather than assume:

```python
import inspect, laya
print(inspect.signature(laya.Agent.__init__))
print(laya.QTYPES)
```

## What this model is and is not good at

Useful context if you are adding a task. Measured on this service:

| Task type | Result |
|---|---|
| Semantic classification (sentiment, spam, intent, content safety) | 8/8 correct |
| Reasoning (arithmetic, temporal, multi-step) | 3/6 correct |

It reads text; it does not compute. If a task needs arithmetic, spatial
reasoning, planning, or search, a deterministic algorithm will beat it on both
accuracy and cost. See [README](README.md#limitations-you-must-not-ignore).

**Zero-shot probabilities are not calibrated.** Do not threshold them as if
they were frequencies without fine-tuning and calibrating on real data.

## License

Contributions are accepted under the Apache License 2.0. See [LICENSE](LICENSE).
By opening a pull request you confirm you have the right to submit the work
under that license.
