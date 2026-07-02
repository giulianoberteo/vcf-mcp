# Changelog

All notable changes to this project are documented in this file.

## [0.2.0] - 2026-07-02

### Changed
- Replaced static `FLEET_API_TOKEN` / `VCFOPS_API_TOKEN` env vars with per-spec
  username/password credentials (`FLEET_USER`/`FLEET_PASSWORD`,
  `VCFOPS_USER`/`VCFOPS_PASSWORD`, optional `VCFOPS_AUTH_SOURCE`). No token is
  ever manually stored — `call_api` now derives the `Authorization` header at
  request time from credentials:
  - `fleet`: HTTP Basic auth built fresh on every call (per Broadcom KB 409715 —
    Fleet Management has no token-acquire endpoint of its own).
  - `vcf-ops`: exchanges credentials for a short-lived OpsToken via
    `POST /api/auth/token/acquire`, caching the token in memory only (never
    written to disk), mirroring `_acquire_ops_token` in
    `privateAI-demo/mcp/server.py`.
- `list_specs()` now reports `credentials_configured` and `auth` type instead
  of `token_configured`.

## [0.1.0] - 2026-07-02

### Added
- Initial git repository setup.
- `.gitignore` (excludes venv, `__pycache__`, `.env`).
- `.env.example` documenting required environment variables (`FLEET_BASE_URL`, `FLEET_API_TOKEN`, `VCFOPS_BASE_URL`, `VCFOPS_API_TOKEN`, `API_TIMEOUT_SECONDS`).

### Fixed
- Moved `vcf-ops-public-api.json` and `vcf-fleet-management-api-docs.json` into `specs/` (renaming the latter to `fleet-management-api-docs.json`) to match the paths `server.py` expects — previously the server would fail to find either spec file at startup.
- Corrected `cd vcf-mcp-server` / example config path in README.md to the actual folder name `vcf-mcp`.
