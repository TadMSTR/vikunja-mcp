# Contributing

Thanks for looking. This is a small, single-maintainer project, so the fastest path from
idea to merged is usually **open an issue first** — especially for anything that adds a
tool, changes a response shape, or touches the credential path.

## Getting set up

```bash
git clone https://github.com/TadMSTR/vikunja-mcp && cd vikunja-mcp
pip install -e ".[dev]"
pytest
```

You do not need a Vikunja instance to develop or to run the test suite — everything is
mocked at the HTTP layer. You need one only for the optional live route sweep below.

Requires Python 3.11+. The project targets 3.11, 3.12 and 3.13, and CI runs all three.

## Before you open a PR

```bash
ruff check src/ tests/ scripts/
ruff format --check src/ tests/ scripts/
pytest --cov=vikunja_mcp --cov-report=term-missing
```

There is a `.pre-commit-config.yaml` if you would rather have that run for you:

```bash
pre-commit install
```

If you changed the Dockerfile, dependencies, or anything on the request path, run the image
smoke test too — it is the same script CI and the release path both run:

```bash
docker build -t vikunja-mcp:dev .
scripts/smoke-image.sh vikunja-mcp:dev
```

If you changed dependencies, refresh the lockfile in the same commit:

```bash
uv lock
```

CI runs `uv lock --check` and will fail if `uv.lock` and `pyproject.toml` disagree.

## What CI will check

| Job | What it means |
|---|---|
| Lint | `ruff check` and `ruff format --check` |
| Test (3.11 / 3.12 / 3.13) | Full suite, plus the coverage floor in `pyproject.toml` |
| Dependency audit | Lockfile in sync, plus runtime and dev advisories, read from the lock and never re-resolved |
| Docker build | Image builds, then all seven smoke checks against it |
| CodeQL | Static analysis of `python` and of the workflows themselves |

## House style

The thing that will feel unusual: **comments here explain *why*, and often name the incident
that caused the code to be the way it is.** A comment that restates the code will get a
review note; one that records a decision, a measurement, or a bug that a future reader would
otherwise re-introduce is exactly what is wanted. Look at `config.py` or `.github/dependabot.yml`
for the register.

A few specifics:

- **Match the surrounding code.** Naming, structure, comment density.
- **Every dependency carries an upper bound.** The cap is the next major, or the next *minor*
  for a 0.x package. Floors are versions actually exercised, not the oldest that might work.
- **Don't lower a gate to make a build green.** The coverage floor and the audit gates are
  ratchets. If one goes red, that is the finding.
- **Prove a new gate two-sided.** Show it failing on the known-bad input as well as passing
  on the known-good one. A gate nobody has seen fail is not known to be a gate — and please
  put that evidence in the PR description.

## Adding a tool

1. Add the function in `server.py`, following the shape of its neighbours.
2. **Pin the HTTP verb with a wire test.** A tool with the wrong verb usually still returns
   something plausible.
3. If it accepts a task reference, add it to `server._TASK_REF_TOOLS` so `#N` resolution
   applies — and preserve the asymmetry: a **bare number is always a global id, never a
   ticket index**. Guessing between the two silently mutated three unrelated tickets once
   already (vikunja#331).
4. Add it to `scripts/verify-routes.py` so the live sweep covers it.
5. Update the tool count in `README.md` if you changed it.

## Things to raise before building

- Adding a credential store, a service account, or any ambient token fallback. The
  token-passthrough model is the central design decision — see
  [ARCHITECTURE.md](ARCHITECTURE.md) — and working around it is almost always the wrong fix.
- Adding config to the `/health` response. It is unauthenticated by design.
- Weakening the webhook SSRF guard. If a target is refused, fix the target.

## Reporting a security issue

Please **do not** open a public issue. See [SECURITY.md](SECURITY.md).

## Commit messages

Present tense, and say *why*. Reference an issue where one exists. The bodies in
`git log` are long by local convention — they record measurements and rejected alternatives
so a later reader does not have to re-derive them. Match that where it is useful; don't pad
where it isn't.

## Licence

By contributing you agree your contributions are licensed under the [MIT Licence](LICENSE).
