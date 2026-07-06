# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial `vikunja-mcp` FastMCP server.
- Token-passthrough auth model: the caller's Vikunja bearer token is read per request and
  forwarded upstream; the server holds no credentials and fails closed on a missing token.
- Tools for projects, tasks (incl. BM25 search), labels + task-label attach/detach,
  comments, saved filters, and project webhooks. Endpoint coverage sourced from the live
  Vikunja Swagger spec (`/api/v1/docs.json`).
- `whoami` for verifying per-agent token wiring through scoped-mcp.
- CI (lint + matrix tests on 3.11–3.13, coverage floor 80%), action versions pinned to SHAs.
