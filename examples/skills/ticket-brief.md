---
name: ticket-brief
description: Turn a selected batch of Vikunja tickets into a durable written brief
  recorded against each ticket, then feed it to the planning step. Use after
  ticket-batch-select and before drafting a build plan. Triggers on "write the brief",
  "brief these tickets", "turn this batch into a plan".
tools: [comment_list, comment_create, task_get]
---

# Ticket Brief

Triage output is a written brief, not a set of labels. The brief is what an agent picking
the work up months later reads instead of reconstructing context from six ticket
descriptions.

Runs after `ticket-batch-select`. Hands off to whatever drafts your build plans.

> Example skill. Replace the artifact path with wherever your team keeps durable build
> notes.

---

## Step 1 — check for a prior brief

    comment_list(task_id)

Run it on each ticket in the batch. A prior brief is recorded as a comment on every ticket
it covers (step 4), so this is the primary check and it is authoritative.

Then also read your brief directory for the files themselves — the comment tells you a brief
exists, the file is the full artifact that build plans reference.

**Read what `comment_list` returns, not just whether a brief is there.** Comments are where
other agents record corrections to a ticket after filing it, and those corrections are
invisible on the ticket body. A ticket's description is what somebody believed when they
wrote it; its comments are what was learned since. Skipping them is how a batch gets built
on a premise that was publicly retracted days earlier.

## Step 2 — verify each premise

For every ticket, before it enters the brief:

- `task_get` it and read the description in full
- read its comments (step 1) — a correction there outranks the description
- check the named file, flag, or service still exists
- if the premise is dead, say so in the brief and drop the ticket from the batch

A ticket filed six weeks ago is a claim about the past. Roughly **a third** of premises in
past reviews had died before anyone picked the ticket up.

Verify by the cheapest thing that would actually fail if the premise were dead — a live
call, a file read. Not by re-reading the ticket that asserted it, and not by re-reading a
config file that lists a tool. **A grant can read as complete on every surface short of
invoking it.**

## Step 3 — write the brief

    ## Brief: <batch name>

    **Tickets:** #A (id N), #B (id M), ...
    **Shared root cause:** <one sentence — the thing that makes this one build>
    **Order, and why:** <what breaks if reordered>
    **Verified:** <what was re-checked, how, and found still true>
    **Dead premises:** <tickets dropped, and what disproved them>
    **Open questions:** <only things that change the work>

Write both numbers on first mention of any ticket: `#42 (id 137)`. `id` is global and what
every tool takes; `identifier` is what the UI shows. They are not offset by a constant, and
a brief that records only one of them costs the next reader a lookup per ticket.

## Step 4 — record it

1. Write the brief to your durable artifact directory — this is what a build plan cites.
2. `comment_create` the brief on **each** ticket in the batch. This is what makes step 1
   work for the next agent, and it is readable in the UI by a human.
3. Hand off to the planning step.

Both surfaces, not one. The file is the artifact; the comments are the index that makes it
discoverable from any ticket in the batch.

## Step 5 — do not close anything

The brief does not transition ticket state. Closing a ticket because a plan was drafted
guesses at intent. Tickets close when the work ships.

---

## Note on grants

This skill's step 1 was originally written to route *around* `comment_list`, because the
agent running it did not hold that tool. When the grant landed, the workaround went away.

The first thing `comment_list` surfaced was a comment posted the previous evening that
nobody had read, carrying a finding that became its own ticket. A brief-checking step routed
around comment reads keeps missing exactly that class of thing.

The general point, for anyone editing this file: **a skill is a claim about the tool surface
at the moment it was written.** Re-verify the assumption against a live call before trusting
it — not against the ticket that requested the grant.
