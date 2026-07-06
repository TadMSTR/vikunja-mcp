# Vikunja structure — project taxonomy contract (proposal)

**Status:** proposed (Phase D design, `vikunja-mcp-capability` build 2026-07-06). No code
depends on this yet. Intended to be published to `host-forge/vikunja-structure.md` (qmd) as
the **single source of truth** once ratified, so both the MCP provisioning tools (Phase B)
and the future CloudCLI plugin read the same contract and cannot drift.

> Data-plane decision (locked): the CloudCLI plugin talks **directly to the Vikunja REST
> API**, not through vikunja-mcp. The MCP is the agent interface; the plugin is the human
> interface; both hit the same Vikunja backend and both honour this taxonomy.

## 1. Project taxonomy

One Vikunja project per tracked unit, mirroring the Plane project set
(`host-forge/plane-projects.md`). Group them under parent projects that match the Plane
sections:

```
Repos — Personal        (parent)
  ├─ agent-bus
  ├─ dockhand-mcp
  └─ … one child per ~/repos/personal/ repo
Repos — Gitea           (parent)
Infrastructure          (parent)   e.g. sandbox-db
Upstream                (parent)   e.g. plane-mcp-server
```

Provisioning uses `project_create(parent_project_id=…)` for the children and
`project_team_add` / `project_user_add` to grant agents access (see §5).

## 2. Identifier scheme

Vikunja has no native `PROJ-N` identifier. Preserve the existing **5-char uppercase Plane
codes** (`AGBUS`, `DHAND`, `GHOST`, …) as the canonical cross-system identifier so links
and references survive the migration:

- Store the code as a **prefix in the project title**: `"[DHAND] dockhand mcp"`.
- Keep the mapping table (code → Vikunja project id) in this doc, updated on each new
  project. `project_list` is the runtime lookup.
- New projects allocate the next code following the current convention (short, unique,
  uppercase, memorable).

## 3. Label set

A fixed label vocabulary, created once via `label_create` and attached with
`task_label_add`. Hex colours are advisory.

| Label | Purpose | Hex |
|-------|---------|-----|
| `type:bug` | defect | `d64545` |
| `type:feature` | new capability | `4c9aff` |
| `type:chore` | maintenance | `9aa0a6` |
| `type:docs` | documentation | `34a853` |
| `type:security` | security finding | `b23c17` |
| `agent-filed` | opened by an agent, not a human | `f4b400` |
| `blocked` | waiting on an external dependency | `ea4335` |

Priority is **not** a label — it maps to Vikunja's native task priority integer
(0=unset … 5=DO NOW), set via `task_create`/`task_update`. Plane priority → Vikunja:
`urgent→4/5, high→3, medium→2, low→1`.

## 4. Views & buckets (kanban)

Each project keeps Vikunja's four auto-created views (`list`, `gantt`, `table`, `kanban`).
The **kanban** view carries the workflow, mapping Plane states → buckets in this order:

| Bucket (column) | Maps from Plane state |
|-----------------|-----------------------|
| `Backlog` | Backlog |
| `Todo` | Unstarted / Todo |
| `In Progress` | Started |
| `In Review` | *(new — PR open)* |
| `Done` | Completed → **done-bucket** |
| `Cancelled` | Cancelled |

Set `Done` as the view's done-bucket via `view_update(done_bucket_id=…)` so dropping a task
there marks it complete. Buckets are created with `bucket_create`; status changes on
migration/automation use `task_bucket_move`.

## 5. Sharing & roles

Projects are shared to agents by **team**, not per-user, so role changes are one edit:

| Team | Permission (Vikunja `Right`) | Members |
|------|------------------------------|---------|
| `agents-read` | 0 (read) | security, writer (read scope) |
| `agents-write` | 1 (write) | research, developer, writer |
| `agents-admin` | 2 (admin) | sysadmin |

Provision with `team_create` + `team_member_add`, then `project_team_add(project_id,
team_id, permission)`. Link shares (`project_share_create`) are reserved for external,
human, read-only access and must set a password (`sharing_type=1`) — never used for agents.

## 6. Open questions (resolve before ratifying)

- Confirm the parent-project grouping is desired vs. a flat list + a `type:` label.
- Decide whether the `[CODE]` title prefix or a dedicated saved filter is the primary way
  humans navigate by identifier.
- Confirm the `In Review` bucket is wanted (no Plane equivalent — it is new workflow).
