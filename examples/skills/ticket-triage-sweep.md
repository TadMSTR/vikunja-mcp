---
name: ticket-triage-sweep
description: Sweep untriaged Vikunja tickets and attach the repo:* label each one belongs to,
  creating missing repo labels as it goes. Use when repo-targeted ticket selection is returning
  a fraction of the real answer, after a batch of new tickets has accumulated, or on a periodic
  triage pass. Triggers on "sweep the untriaged tickets", "backfill repo labels", "triage the
  backlog", "why does repo-targeted selection return so few tickets".
tools: [label_list, label_create, task_label_add, task_label_remove, task_list, task_get, backlog_summary]
---

# Ticket Triage Sweep

Attaches a `repo:*` label to every ticket that has none, so repo-targeted selection works.
Triage only — it never edits ticket text, status, or priority.

> Example skill. Replace `<project>` with your project id and `<anchor>` with your
> vocabulary-anchor task id. Every number below came out of one real full-corpus run and is
> kept because it shows why the rule exists — re-derive rather than quoting.

---

## Derive at runtime what changes at runtime

The project id is the one constant. **Never hardcode a label id set** — this procedure
creates labels, so any list you inline is stale by your own step 3.

    labels = []                                  # page until exhausted, do NOT pass a
    page = 1                                     # single large per_page and hope
    while True:
        r = label_list(per_page=50, page=page)
        labels += r["items"]
        if page >= r["pagination"]["total_pages"]: break
        page += 1
    repo_ids = [l["id"] for l in labels if l["title"].startswith("repo:")]

**`label_list` paginates, and a too-small `per_page` under-returns silently.** This file
said `per_page=60` until the label set reached 67 and page 2 held **seven** `repo:` labels —
every one created by that day's backfill. Read `pagination.total_pages` and loop; raising 60
to 100 just moves the cliff. The failure is fail-safe but misleading: a short `repo_ids`
makes the untriaged query return the tickets carrying the missing labels, which reads as
"the sweep did not finish".

Worse than stale: label ids are *per-agent*. Vikunja shows an account only the labels it
created plus those attached to a task it can read, so `label_list` returns a **different set
to different agents on the same tracker at the same moment** — 41 versus 39, measured. A
hardcoded list is wrong for somebody the day it is written.

Snapshot after one full sweep, to re-derive rather than quote: **54 repo labels**, 391
tickets triaged, 48 carrying `repo:none`.

## `repo:none` is inside the repo set, and that is the whole design

`repo:none` marks *"read by a sweep; genuinely has no repo"* — pure infra/ops work.

Because its title starts with `repo:`, the runtime derivation above **includes it**. So:

> untriaged = carries no `repo:*` label = **never triaged**

not "has no repo". That is what makes the sweep resumable and idempotent: a
triaged-but-repoless ticket drops out of the query permanently instead of being re-read on
every future run. Without it the sweep re-reads the same ~50 infra tickets forever.

Consequence for verification: a completed sweep drives the untriaged count to **0**, not to
the `repo:none` count. If you expected the latter, you derived `repo_ids` without
`repo:none`.

---

## Step 0 — baseline, with its own negative control

    backlog_summary(project_id=<project>,
                    filter='labels not in <repo_ids>',
                    max_label_buckets=<len(labels) + headroom>)

**Size the bucket cap from `label_list`.** Do not inline a number — this procedure creates
labels, so any cap you hardcode is undersized by your own step 3.

**Check the negative control before believing the number:** every `repo:*` bucket in that
response must read **0**. If any is non-zero the `not in` clause was silently dropped and
the count is meaningless — a filter Vikunja ignores returns everything, which reads like
success.

**Before reading any bucket, check that it was counted at all.** Three conditions, all of
them — no single one holds against both an older server and a current one:

1. **Every `repo:*` title from `label_list` is present as a key in `by_label`.** An absent
   key is a bucket that was never counted, and `by_label.get(title, 0)` turns that into a
   `0` — "not measured" rendered as "nothing here". Absence must **fail** the control.
2. **`labels_truncated` is `false`.**
3. **`labels_not_counted` is `[]`** when the key is present — it names the skipped buckets.
   Servers predating vikunja-mcp v0.10.0 do not return it; its *absence* is not a pass.

`not_done` and `total` are unaffected by any of this. Read them as the load-bearing number
and treat the buckets as confirmation.

**An earlier version of this file said "a truncated bucket is reported as `0`, not omitted".
That was wrong, and the wrong diagnosis is why the guard it justified did not work.**
Over-cap buckets were always omitted. The `0` reading came from elsewhere: `by_label` is
keyed by title, titles are not unique, so two labels sharing a title collided into one key
and the survivor depended on where the cap fell — 0 at cap 25, 9 at cap 45, same backlog.
Fixed in v0.10.0. **Condition 1 is the one that holds against an older server.**

Read `not_done` (and `total` with `include_done=true`). Record it; step 6 compares against it.

`backlog_summary` **already injects** the anchor exclusion and `done = false` unless you
pass `include_done`. `task_list` injects nothing — write both clauses yourself.

## Step 1 — the marker label must exist first

If `repo:none` is not in `repo_ids`, create it and **attach it to the anchor task in the
same step** (see step 3). Do this before any sweeping. Without the marker the run is not
resumable, and an interrupted sweep leaves no way to tell read-and-repoless from never-read.

Creating it is also a live write test of your `label_create` grant. Better to fail on call
one than 200 calls in — **a config file that lists a tool is not proof the token carries the
permission.** That exact gap once read clean on every surface while the tool 401'd.

## Step 2 — build the naming authority

List your local checkouts. Directory names are the **naming authority** — they stop you
inventing `repo:cli` for a repo actually called `cli-plugin-task-queue`.

Not every repo is cloned. Where one is not, name the label from the remote path and **say so
in the label description** rather than silently guessing.

## Step 3 — create labels lazily, and seed every one onto the anchor

**Do not pre-create a label per local repo.** In one measured case 71 repos existed against
28 pre-existing labels; creating all of them would have added ~45 labels for repos that have
never had a ticket, cluttering every agent's `label_list` permanently. Create a label only
when a ticket you are reading actually names that repo. The first sweep created **25** this
way, against 28 that already existed.

Every `label_create` is followed **immediately** by:

    task_label_add(task_id=<anchor>, label_id=<new>)

The anchor task exists for exactly this. Vikunja lets an account attach a label only if it
created that label *or* the label already sits on a task it can read. **Seeding is what
makes a label you created visible to every other agent** — skip it and the skills that
consume your output cannot see the labels this sweep just created, which defeats the sweep.

Seed pre-existing repo labels onto the anchor too. Before the first sweep the anchor carried
10 labels; 27 older `repo:*` labels were attachable only because each happened to sit on
some other readable ticket — incidental, not guaranteed. A cleanup closing the last ticket
carrying a rare label would silently break attachability for everyone but its creator.

Never strip or delete the anchor task.

## Step 4 — sweep, newest-first, one page at a time

    task_list(filter='project = <project> && done = false && labels not in <repo_ids>',
              sort_by='id', order_by='desc', per_page=50, page=1)

**Newest-first**, so an interrupted run has finished the half most likely to be selected
against.

**Always re-query page 1. Never paginate.** The filter is self-consuming: labelling a ticket
removes it from its own result set, so page 1 returns the next 50 untriaged every time.
Walking `page=2, 3…` against a shrinking set skips tickets silently. This is also why an
aborted run needs no bookmark — it recomputes the remaining set from the query, not from a
position.

Per ticket, decide the repo(s) and apply one `task_label_add` per label. Many-to-many is
normal; some tickets name three repos.

### Bulk writes are impossible — do not go looking

`tasks_bulk_update` with `values={"labels": [...]}` returns **`400 The task field 'labels'
is invalid` (Vikunja code 4027)**. A 400 naming the field, not a 401 — a schema limit no
grant widens. Positive control the same minute: `{"priority": 2}` → 200.

Budget **one call per ticket per label**. The first full sweep cost ~470 `task_label_add`
calls for 391 tickets.

## Step 5 — deciding the repo (this is where the errors are)

`task_list` returns no description. Titles are usually enough, but three traps produced
every mistake in the first run:

**Never infer a repo from a deployment artifact name.** A venv path, process name, or
container name is not a repo name. One ticket naming a venv directory was labelled from that
path — and the description said the venv serves a *different* package entirely. When
a title names an artifact rather than a repo, `task_get` it, or locate the file on disk.
That check moved three tickets that would all have gone to the wrong repo on their names
alone. Watch for symlinks, too — a path that looks like it belongs to one repo can resolve
into another.

**"Found during X" is not "fixed in X".** A ticket found while reviewing one repo may be
fixed by a host-level change that is no repo's code. Conversely, a ticket with a repo name
in its *title* may say outright in its description that the fix lands elsewhere.

**Code moves between repos.** A script split out into its own repo leaves older tickets
referencing the old path. Check where the file is now.

Spot-check five of your hardest calls afterwards by `task_get`. The first run's five-ticket
check — all deliberately chosen as the riskiest — found **one wrong out of five**. A rate
worth measuring rather than assuming.

### When `repo:none` is right

Roughly one ticket in eight (48 of 391). Genuine cases: host-level infra with no tracked
config, tracker or data curation, fleet-wide policy questions naming no specific repo, and
work on a repo that does not exist yet.

**Fleet-wide tickets that name specific repos get those repos, not `repo:none`** —
`repo:none` asserts *no repo applies*, so mixing it with a real repo label is contradictory.

Be more willing to attach a repo than the ticket's own framing suggests. A docs ticket is
closed by editing whichever repo holds the doc, so it belongs to that repo even though it
reads as pure ops. The test is **"which repo does a fix land in"**, not "does this feel like
code".

## Step 6 — verify at BOTH agent boundaries

**As the sweeping agent:** re-run step 0. `not_done` should be **0**, with every `repo:*`
bucket still reading 0. Run again with `include_done=true` if you swept closed tickets.

**As the consuming agent — this is the one that matters.** The sweep runs as one agent; the
selection skills run as another. Have that agent run `label_list` and confirm it sees every
label the sweep created, then run the same untriaged query and get the same answer. You
cannot do this yourself: calling as another agent is refused as impersonation, correctly.

A backfill correct only from the sweeper's side has fixed nothing. **The check has to cross
the boundary the consumer sits on.**

## Step 7 — sweep closed tickets, or say you did not

Same query with `done = true`. Lower value: only the build-session-cohort selection mode
reads closed tickets. Separately abortable — do it only after the open sweep is clean, and
if you skip it, **say so in the output**. A count that silently covers only open tickets
reads as covering the corpus.

## Step 8 — leave the snapshot correctable

`ticket-batch-select` measures coverage at runtime and self-corrects, but it carries a dated
snapshot and threshold wording. Re-read it after a sweep and update the figures.

Always write both numbers on first mention: `#42 (id 137)`. `id` is global and what every
tool takes; `identifier` is per-project and what the UI shows. They are not offset by a
constant.

---

## Tools used

`label_list`, `label_create`, `task_label_add`, `task_label_remove`, `task_list`, `task_get`,
`backlog_summary`. `task_label_remove` is only for correcting a mislabel found in step 5.

**Verify a grant by calling it, not by reading a config file.**
