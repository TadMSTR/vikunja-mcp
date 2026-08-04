# Vikunja structure — see the knowledge base

This file used to hold an 83-line copy of forge's Vikunja taxonomy. It had drifted from
the ratified version and, critically, was missing the label-ID table that every agent's
`CLAUDE.md` points at — so an agent reading it here got an incomplete answer while
believing it had the authority.

**Single source of truth:**

- `~/repos/gitea/host-forge-knowledge-base/vikunja-structure.md` (155 lines, ratified)
- Or via qmd: `qmd_get(file="host-forge/vikunja-structure.md")`

It covers the flat project model (`project_id=7`, `Homelab-Agent`), the label vocabulary
with IDs (`type:*`, `agent-filed`, `source:*`), and the priority mapping.

Nothing about that taxonomy is specific to this repo, which is why it does not live here.
