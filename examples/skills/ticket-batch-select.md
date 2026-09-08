---
name: ticket-batch-select
description: Select a coherent batch of Vikunja tickets to build against. Use when
  starting a build session and deciding what to work on — whether nothing specific is
  targeted, a single repo is being closed out, or a previous build session's tickets
  need review. Triggers on "what should I work on", "find tickets I can close
  together", "what's open for <repo>", "what did the last build leave behind".
tools: [backlog_summary, label_list, task_list, task_get, task_search]
---

# Ticket Batch Select

Produces a ranked shortlist with an explicit reason the tickets belong together.
Never a score. Selection only — drafting the plan is a separate step.

> Example skill. Replace `<project>` with your project id, and `<anchor>` with the id
> of your vocabulary-anchor task if you use one (see `ticket-triage-sweep.md`). Every
> number quoted below is a measurement from a real run, kept because it shows why the
> rule exists — re-derive rather than trusting it.

---

## Derive at runtime what changes at runtime

The project id is the one constant. Hardcode nothing else — above all not a label id set.

Three separate drifts invalidated the first draft of this skill within ~30 hours, on text
nobody edited. One was a label the triage procedure itself creates. **Label ids are runtime
state.**

Worse, they are *per-agent* runtime state. Vikunja shows an account only the labels it
created plus those attached to a task it can read, so `label_list` returns a different set
to different agents — measured at 41 labels for one agent and 39 for another, on the same
tracker at the same moment. A hardcoded id list is not merely stale-prone; it is wrong for
somebody the day it is written.

Build the repo label set like this, every run:

    labels = []                                  # page until exhausted, do NOT pass a
    page = 1                                     # single large per_page and hope
    while True:
        r = label_list(per_page=50, page=page)
        labels += r["items"]
        if page >= r["pagination"]["total_pages"]: break
        page += 1
    repo_ids = [l["id"] for l in labels if l["title"].startswith("repo:")]

**`label_list` paginates, and a too-small `per_page` under-returns silently.** This file
said `per_page=60` until the label set reached 67 and page 2 held **seven** `repo:` labels.
Read `pagination.total_pages` and loop; raising 60 to 100 just moves the cliff. The failure
is fail-safe but misleading: a short `repo_ids` makes the untriaged query return the tickets
carrying the missing labels, which reads as "the sweep did not finish".

Measured once as a **snapshot, not a constant**: 54 repo labels, 619 tickets, 290 open. The
set grew from 28 to 54 in a single day when a backfill ran. Re-derive.

---

## Step 1 — orient

    backlog_summary(project_id=<project>, max_label_buckets=<len(labels) + headroom>)

**Size this from `label_list`, do not hardcode it.** This file said "always pass 45" until a
backfill took the label set to 65 and silently invalidated it — a hardcoded bucket cap is
the same defect as a hardcoded id list.

**Before reading any bucket, check that it was counted at all.** Three conditions, all of
them — no single one holds against both an older server and a current one:

1. **Every `repo:*` title from `label_list` is present as a key in `by_label`.** An absent
   key is a bucket that was never counted, and `by_label.get(title, 0)` turns that into a
   `0` — "not measured" rendered as "nothing here", which is precisely what this control is
   looking for. Absence must **fail** the control, never pass it.
2. **`labels_truncated` is `false`.**
3. **`labels_not_counted` is `[]`** when the key is present — it names the skipped buckets.
   Servers predating vikunja-mcp v0.10.0 do not return it; its *absence* is not a pass.

`not_done` and `total` are unaffected by any of this. Read them as the load-bearing number
and treat the buckets as confirmation.

**An earlier version of this file said "a truncated bucket is reported as `0`, not
omitted". That was wrong, and the wrong diagnosis is why the guard it justified did not
work.** Over-cap buckets were always omitted. The `0` reading that prompted it came from
somewhere else entirely: `by_label` is keyed by title, and titles are not unique — two
labels sharing a title collided into one key, and which one survived depended on where the
cap happened to fall (0 at cap 25, 9 at cap 45, same backlog). Fixed in vikunja-mcp v0.10.0
by counting every id sharing a title as a single bucket, along with a second defect where
`labels_truncated` was computed against the labels that had been *fetched* rather than the
ones that exist, and so read `false` while 15 went uncounted. **Condition 1 is the one that
holds against a server predating those fixes** — check the deployed version before relying
on 2 or 3 alone.

Read `scope.filter` back. `backlog_summary` **already injects** the anchor exclusion and
`done = false` unless you pass `include_done`. You do not need to add those here — but you
do in `task_list`, which injects nothing.

Two traps in the numbers it returns:

- **`by_label` is keyed by title, not id.** Duplicate titles collapse into one bucket. From
  v0.10.0 that bucket is counted over *every* id sharing the title, so it means what it
  says; before that it reported whichever colliding id fell inside the cap.
- **`by_staleness` may be dead.** A tracker migration that rewrites `updated` on every
  ticket makes `stale` read 0 for everything at the 90-day default. Check before ranking on
  it.

## Step 2 — pick a mode

### Mode A — thematic (nothing specific targeted)

Pick the densest bucket from step 1, then pull rows:

    task_list(filter='project = <project> && done = false && labels in <id> && id != <anchor>',
              sort_by='priority', order_by='desc', per_page=50)

Coherence test before shortlisting — a batch is real only if the tickets share a file, a
service, or a root cause. "Both are `type:bug`" is not coherence. State the shared thing in
one sentence; if you cannot, the batch is not a batch.

### Mode B — repo-targeted

    task_list(filter='project = <project> && done = false && labels in <repo id>')

**Measure coverage first — do not assume it, and do not trust this file's assertion of
it.** A backfill can take the untriaged count to zero, but new tickets arrive unlabelled, so
coverage decays from the day it is fixed. Derive it:

    repo_ids = <paginate label_list as above> -> titles starting "repo:"
    backlog_summary(filter='project = <project> && labels not in <repo_ids>',
                    max_label_buckets=<len(labels) + headroom>)

Read `not_done` from that. It is the number of open tickets no repo filter can reach.
Sanity-check it: every `repo:*` bucket in that response must read 0. If one does not, the
`not in` clause was not applied and the count is meaningless.

**Apply the three-condition bucket check from step 1 before trusting that control** — an
absent bucket read as `0` makes this control pass vacuously. `not_done` itself is
unaffected; the buckets are what need the headroom.

Note `repo:none` is itself a `repo:` label, so it lands in `repo_ids` and a
triaged-but-repoless ticket correctly does not count as uncovered. `not_done` here means
**never triaged**.

- **0, or a handful** → Mode B is the good path. Use it.
- **Growing** → those tickets are unreachable by any repo filter. Say so plainly in the
  output; a shortlist that hides its blind spot reads as complete. Run the triage sweep to
  clear them rather than working around the gap.

Only when coverage is poor, fall back to:

    task_search(query="<repo name>")

and treat the result as a starting point, not an answer. It matches title **and**
description, so it over-matches badly — measured once at **475** results for a repo name
against **15** open tickets actually carrying that repo's label. Nearly all of it was prose
mentions in unrelated tickets. Read before shortlisting (step 3) is not optional here.

### Mode C — previous build session

By creation window (the reliable one):

    task_list(filter='project = <project> && created > "YYYY-MM-DD" && created < "YYYY-MM-DD"',
              per_page=50)

Or by build-plan path, when the session wrote one into the descriptions:

    task_list(filter='project = <project> && description like "%build-plans/<name>%"')

This returns done **and** open tickets — which is what you want here. The cohort's closed
members are what tell you what the followup is.

Caveat: if your tracker was migrated, every ticket created before the migration has
`created` flattened to the migration date, so windowing only resolves work since then.

## Step 3 — read before shortlisting

`task_list` returns projected rows with **no description**. Before putting a ticket on the
shortlist, `task_get` it. Two things matter and neither is in the projection:

- the "## Related" block, which is prose. `related_tasks` is populated on some tickets and
  empty on most, so the dependency graph is only reliably readable by parsing descriptions.
- whether the ticket's premise is still true. Ticket text is a claim about the past, and a
  stale one sends the whole batch the wrong way.

## Step 4 — output

A table: identifier + id, title, type, priority, and one column saying why it is in the
batch. Then one sentence naming the shared root cause. Then what you excluded and why — a
shortlist that hides its rejects reads as "this is everything".

If Mode B ran against poor coverage, or Mode C against pre-migration dates, state that limit
in the output. Silent truncation reads as completeness.

Always write both numbers on first mention: `#42 (id 137)`. `id` is global and what every
tool takes; `identifier` is per-project and what the UI shows. **They are not offset by a
constant.**

---

## Verifying a filter you did not get from this file

Every filter above was run against a live tracker with a negative control. If you write a
new one, do the same — **a filter Vikunja silently ignores returns everything, which reads
like success.**

Use a control string that does not exist in the corpus, and generate a fresh one. Do not
reuse a recorded control: the draft of this skill used a fixed nonsense string, and by the
time it was installed that filter no longer returned empty — the ticket documenting the
control had itself entered the corpus and matched it. **A control published in the tracker
stops being a control.**
