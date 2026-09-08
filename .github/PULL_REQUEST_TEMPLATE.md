## What and why

<!-- What changes, and what problem it solves. If there is an issue, link it. -->

## How it was verified

<!--
Not "tests pass" — what did you actually run, and what did it say?

If this PR adds or changes a GATE (a test, a CI check, an audit step, a validation), show
it BOTH ways: failing on the known-bad input as well as passing on the known-good one. A
gate nobody has seen fail is not known to be a gate, and this repo has caught more than one
green check that was measuring nothing.
-->

## Checklist

- [ ] `ruff check src/ tests/ scripts/` and `ruff format --check src/ tests/ scripts/` pass
- [ ] `pytest` passes, and coverage is at or above the floor in `pyproject.toml`
- [ ] Dependencies changed? `uv lock` re-run and `uv.lock` committed in the same commit
- [ ] Request path, Dockerfile or dependencies changed? `scripts/smoke-image.sh` run against a local build
- [ ] New tool? Verb pinned by a wire test, added to `scripts/verify-routes.py`, and README's tool count updated
- [ ] Comments explain *why* — a decision, a measurement, or a bug someone would otherwise re-introduce
- [ ] CHANGELOG.md updated

## Anything reviewers should look at closely

<!-- Trade-offs you were unsure about, alternatives you rejected, or anything you could not test. -->
