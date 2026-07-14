# Vikunja structure — project taxonomy contract

**Status:** RATIFIED (2026-07-07, decided in-session with research; provisioned
2026-07-14, build task `60b7a44b`). Published to `host-forge/vikunja-structure.md` (qmd)
as the **single source of truth**, so both the MCP provisioning tools (Phase B) and the
future CloudCLI plugin read the same contract and cannot drift.

> Data-plane decision (locked): the CloudCLI plugin talks **directly to the Vikunja REST
> API**, not through vikunja-mcp. The MCP is the agent interface; the plugin is the human
> interface; both hit the same Vikunja backend and both honour this taxonomy.

## 1. Project taxonomy

**Flat model — one shared project, not a per-repo hierarchy.** The PROPOSED
project-per-repo taxonomy (parent projects mirroring the Plane project set) was rejected:
Vikunja isn't scoped to code-repo work the way Plane is — it's the general place any agent
opens an item for anything forge/homelab-agent related (infra, ops, ideas, docs, code)
without a per-repo provisioning step.

One shared project: **`Homelab-Agent`** (id 7). Writable by all agent roles via the
`agents-write` team (§5). Covers everything — no sub-project split by domain.

Per-agent personal Inboxes (each agent has its own — e.g. developer = project id 3) are
kept as-is, untouched by this taxonomy. No defined use case yet.

Child projects (Vikunja supports real parent/child hierarchy) are reserved **only** for
genuinely large multi-phase efforts. None are pre-created — add one later only if a
specific effort needs it.

## 2. Label set

Existing label vocabulary, created via `label_create` and attached with `task_label_add`.
Hex colours are advisory.

| Label | Purpose | Hex |
|-------|---------|-----|
| `type:bug` | defect | `d64545` |
| `type:feature` | new capability | `4c9aff` |
| `type:chore` | maintenance | `9aa0a6` |
| `type:docs` | documentation | `34a853` |
| `type:security` | security finding | `b23c17` |
| `agent-filed` | opened by an agent, not a human | `f4b400` |
| `blocked` | waiting on an external dependency | `ea4335` |
| `source:github` | task relates to a GitHub-hosted code repo | `2088ff` |
| `source:gitea` | task relates to a Gitea-hosted docs/config repo | `609926` |

Absence of a `source:*` label means a pure homelab/infra/ops item with no repo attached.
Per-repo labels (e.g. `repo:vikunja-mcp`) are **not** pre-provisioned — create ad hoc, on
demand, only if filtering by a specific repo is actually needed.

Priority is **not** a label — it maps to Vikunja's native task priority integer
(0=unset … 5=DO NOW), set via `task_create`/`task_update`. Plane priority → Vikunja:
`urgent→4/5, high→3, medium→2, low→1`.

## 3. Views & buckets (kanban)

Use Vikunja's auto-created default kanban columns as-is — no custom bucket set, no
Plane-state-to-bucket mapping, no `In Review` bucket. If a code task needs a "PR open"
signal, the PR itself is the source of truth; that state is not duplicated in Vikunja.

## 4. Sharing & roles

Projects are shared to agents by **team**, not per-user, so role changes are one edit.
Provisioned 2026-07-14 (build task `60b7a44b`):

| Team | Vikunja team id | Permission (`Right`) | Members |
|------|-----------------|-----------------------|---------|
| `agents-read` | 1 | 0 (read) | security, writer |
| `agents-write` | 2 | 1 (write) | research, developer, writer |
| `agents-admin` | 3 | 2 (admin) | sysadmin |

`Homelab-Agent` (project id 7) is shared to `agents-write`. Team creators are
auto-added as team admin by Vikunja on `team_create` — developer (who provisioned these
teams) is an admin member of all three in addition to the roles listed above.

Provision with `team_create` + `team_member_add`, then `project_team_add(project_id,
team_id, permission)`. Note: Vikunja Personal Access Tokens are scoped per route-group at
creation — `teams`, `projects_teams`, and `teams_members` are three **separate** scope
groups that must each be explicitly granted, or the corresponding endpoints 401 even with
an otherwise-valid token (see `HLAGNT-9`/`HLAGNT-10`).

Link shares (`project_share_create`) are reserved for external, human, read-only access
and must set a password (`sharing_type=1`) — never used for agents.
