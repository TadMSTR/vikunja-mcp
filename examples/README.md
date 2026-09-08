# Examples

Everything here is exercised by CI on every pull request
([`scripts/check-examples.sh`](../scripts/check-examples.sh)). That is deliberate: compose
examples rot precisely because nothing runs them, so they stay syntactically present and
functionally wrong.

## `compose/`

| | |
|---|---|
| [`minimal/`](compose/minimal/) | The smallest thing that runs. Two decisions: where Vikunja is, and who can reach the port |
| [`full/`](compose/full/) | Every option written out and explained, including the audit-log volume |

Both are stood up by CI against the image built from the commit under review, and probed on
`/health`. The full example's audit-log mount is additionally asserted to be writable by the
container's uid — a read-only root plus a wrong-owner mount fails closed at the *first
write*, long after startup, which is exactly what an example gets wrong silently.

Neither file sets `container_name`. It is host-global, so a fixed name in a file people copy
would collide with any other container of that name on the machine — including a production
one.

## `skills/`

Agent skills for the ticket-management workflow these tools were built for: select a
coherent batch, brief it, keep the labels clean.

| | |
|---|---|
| [`ticket-batch-select.md`](skills/ticket-batch-select.md) | Pick a coherent batch of tickets to work on |
| [`ticket-brief.md`](skills/ticket-brief.md) | Turn a batch into a durable written brief recorded on each ticket |
| [`ticket-triage-sweep.md`](skills/ticket-triage-sweep.md) | Backfill `repo:*` labels so repo-targeted selection works |

They are markdown and cannot be executed, so CI validates their frontmatter and links, and —
the check with actual value — asserts that **every tool name they reference still exists**,
read from a live `tools/list` against the image. A skill pointing at a renamed tool is broken
in a way no markdown linting detects, and it fails at use time in somebody else's session.

Adapt rather than adopt: they carry placeholder project ids, and the measurements in them
are from real runs on one tracker. The reasoning transfers; the numbers do not.
