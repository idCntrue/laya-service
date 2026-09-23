<!--
Thanks for contributing. Keep the diff focused -- unrelated reformatting makes
a change hard to review and will be asked to be split out.
-->

## What changed

<!-- One or two sentences. What is different after this PR? -->

## Why

<!-- The problem this solves. Link the issue if there is one: Fixes #123 -->

## How this was verified

<!--
Do not write "tests pass". Say what you ran and what you saw. If you changed
behaviour, show the before/after. If you fixed a bug, name the test that fails
without this change.
-->

```
$ make check
...
```

## Checklist

- [ ] `make check` passes (lint, format-check, typecheck, test)
- [ ] New or changed public functions/classes have Google-style docstrings
- [ ] Bug fixes come with a test that fails without the fix
- [ ] Layering rules respected — `domain` imports only stdlib, `application`
      imports no framework, `laya`/`torch` appear only in
      `infrastructure/model/laya_adapter.py`
- [ ] If the HTTP API changed: `API.md` **and** `API.en.md` updated, plus the
      OpenAPI descriptions in `interfaces/http/schemas/`
- [ ] If a config option was added: `.env.example`, `Settings`, and the README
      configuration table all updated
- [ ] `CHANGELOG.md` updated under `[Unreleased]`

## Breaking change?

<!--
Yes/No. If yes, say what breaks and what a caller must do. This project is
pre-1.0, so minor bumps may break -- but it must be stated explicitly.
-->

## Anything reviewers should look at closely

<!--
Optional. Point at the part you are least sure about. That is more useful than
a summary of the parts you are confident in.
-->
