# Security Audit

Every release of this project is audited before merge by a reviewer independent of the agent
that wrote it. This page records what those audits found and what happened next.

## Audit History

| Date | Version / Scope | Findings | Status |
|------|-----------------|----------|--------|
| 2026-07-06 | v0.2.0 — initial capability review | 0 | Clean |
| 2026-07-19 | v0.2.2 — markdown → HTML rendering | 0C / 0H / 1M / 1L | Remediated before merge |
| 2026-07-19 | v0.2.2 — `task_update` full-replace fix | 0C / 0H / 0M / 1L (1 info) | Remediated before merge |
| 2026-08-04 | v0.3.0 — hardening round 1 | 0C / 0H / 1M / 2L (3 info) | Remediated before merge |
| 2026-08-11 | v0.4.0 — hardening round 2 | 0 (2 info) | Clean |
| 2026-08-21 | v0.5.0 — ticket-reference resolution | 0 (2 info) | Clean |
| 2026-08-21 | v0.6.0 — Docker deployment, `/health`, stdio | 0C / 0H / 0M / 1L (2 info) | Remediated before merge |
| 2026-08-22 | v0.7.0 — agent affordances | 0C / 0H / 1M / 0L | Remediated before merge |
| 2026-08-22 | v0.8.0 — structured ticket metadata | **0C / 1H** / 0M / 0L | Remediated before merge (`428cfcf`) |
| 2026-08-23 | v0.9.0 — Vikunja API v2 port | 0C / 0H / 0M / 1L | Remediated before merge |
| 2026-09-02 | v0.10.0 — truthful signals | 0C / 0H / 0M / 2L (1 info) | Remediated before merge |
| 2026-09-08 | v0.11.0 — flagship promotion | 0 (7 info) | Clean |

## Summary

**No audit has ever found a Critical finding.** One High was found, in the v0.8.0 audit, and
it was fixed before that release merged.

That High is the one worth knowing about, because the class it belongs to recurs. Structured
markers embedded in task descriptions could be **forged by ordinary prose**: a description
containing text that merely *looked* like a marker footer was parsed as a genuine one, which
bypassed URL validation on `linked_refs` and undermined idempotency-key integrity. A ticket
*documenting* the marker format was indistinguishable from a ticket *using* it — and the same
defect silently deleted such a paragraph from every read projection while storage still held
it, so anyone pasting `docs/markers.md` into a ticket hit it.

Two changes fixed it, both in `428cfcf`: every URL is now re-validated **on read** with the
same predicate the write path uses (deliberately the same guard, not a stricter one — a
stricter read would silently eat links this server itself wrote, and a write/read parity test
asserts that it does not), and the marker footer must be the **trailing** block, position
being the only structural signal available to separate the collision that actually happens.

Everything else has been Medium or below and remediated in the same session it was found.
Recurring themes across the Medium and Low findings: response-shaping paths that could leak
more than intended, validation applied on one side of a read/write pair but not the other,
and gates that could not actually fail.

## v0.11.0 — flagship promotion (2026-09-08)

Clean: no findings at Low or above. Seven informational items, all of which were risks this
build **self-disclosed in its own audit request** and asked to have checked independently.
The one that mattered:

**Ticket-reference widening.** `other_task_id` (both relation tools) and `task_ids`
(`tasks_bulk_update`) changed from `int` to `int | str`, which moved validation off the
schema layer and onto the `_resolve_task_ref_kwarg` before-hook. `task_relation_remove`
interpolates `other_task_id` straight into a URL path, so if that hook were ever skippable an
attacker-controlled string would reach the interpolation unresolved.

The auditor traced the call path rather than taking the build's word for it, and found the
hook structurally unbypassable: `@tool` resolves to `mcp.tool()(instrument(fn))`, so the name
bound at module scope *is* the wrapped version and there is no bare, hook-free callable to
import; `instrument()`'s wrapper calls `run_before_hooks` unconditionally; and
`register_builtin_hooks()` runs at import, outside any function or conditional. Both relation
tools and `tasks_bulk_update` are in `_TASK_REF_TOOLS`, and `_resolve_task_ref` either returns
an `int` or raises — it never falls through to returning the original string.

The build's own probe (12 hostile inputs — path traversal, percent-encoded traversal, SQL-ish,
bool, float, `None`, list, dict — all refused with zero upstream calls) and the auditor's
independent trace agree. Regression coverage is in `tests/test_task_refs.py`.

Also verified and clean: sanitization of three agent skills published in `examples/skills/`,
topology removal, credential literals in tests and docs (placeholders only), `release.yml`'s
`id-token`/`attestations` permissions (scoped to the single publishing job), the
`_blank_project_id_is_unset` config validator, and the two new CI shell scripts.

**What the audit did not run:** the container image build, live execution of
`scripts/smoke-image.sh` and `scripts/check-examples.sh`, and the OSSF Scorecard score. The
script logic was reviewed and found sound, and CI runs all three on every pull request — but
that is a gap in depth, recorded here rather than left implicit.

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md). Please do not open a public issue.

## Related

- [SECURITY.md](../SECURITY.md) — the credential model, the SSRF guard, and the rules a change should not break
- [../ARCHITECTURE.md](../ARCHITECTURE.md) — the six invariants each audit re-checks
- [../CHANGELOG.md](../CHANGELOG.md) — what shipped in each version above
