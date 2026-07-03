# Changelog

All notable changes to this project are documented in this file.

## [0.3.5] - 2026-07-03

### Changed
- Trimmed the `search_endpoints` comments back down — the previous pass
  over-explained it line by line. No behavior change.

## [0.3.4] - 2026-07-02

### Changed
- Expanded the comments inside `search_endpoints` to walk through the
  scoring logic step by step (building the haystack, substring matching,
  ranking by match count, reshaping into the compact summary) — no
  behavior change.

## [0.3.3] - 2026-07-02

### Changed
- Added a "How a spec file becomes a working API call" section to the
  README explaining the actual mechanism — spec normalization
  (`openapi_utils.load_and_normalize_spec`), keyword-based endpoint
  discovery (`search_endpoints`/`get_endpoint`), and spec-driven HTTP
  request construction (`call_api`) — so it's clear none of this is
  generated or hand-wired per endpoint.
- Fixed a stale "Both spec files" reference in the intro (now three specs).

## [0.3.2] - 2026-07-02

### Changed
- Finished externalizing per-spec constants into `config.py` — the server
  name (`SERVER_NAME`, overridable via `MCP_SERVER_NAME`), each spec's
  token-acquire path (`token_path`), the response field the token comes
  back in (`token_response_field`), and the `Authorization` header scheme
  (`auth_scheme`: `Basic`/`OpsToken`/`Bearer`) are now all config, not
  literals in `server.py`.
- Collapsed `_acquire_ops_token` and `_acquire_sddc_token` into a single
  `_acquire_token`, since both were the same POST-credentials-cache-token
  shape once the path/field name moved into config.

## [0.3.1] - 2026-07-02

### Changed
- Renamed the FastMCP server instance from the stale `"vcf-fleet-api"` to
  `"vcf-mcp"`, matching the project/repo name.

## [0.3.0] - 2026-07-02

### Added
- New **`sddc`** spec: VCF (SDDC Manager) API, 375 operations, from
  `specs/vmware-cloud-foundation.json`. Authenticates by exchanging
  `SDDC_USER`/`SDDC_PASSWORD` for a bearer access token via `POST
  /v1/tokens` (same acquire-once-and-cache-in-memory pattern as `vcf-ops`,
  just a different endpoint/response shape — `accessToken` instead of
  `token`). New env vars: `SDDC_BASE_URL`, `SDDC_USER`, `SDDC_PASSWORD`,
  `SDDC_VERIFY_SSL`.
- `config.py`: pulled `SPECS`, `TIMEOUT`, and `verify_ssl()` out of
  `server.py` into a dedicated configuration module, so wiring in a new VCF
  API only means adding a spec file + one `SPECS` entry — no changes to
  request-building or MCP tool logic.

### Verified
- Live end-to-end against the lab: `sddc` (`getNtpConfiguration`) returned
  `200` using the new bearer-token auth flow.

## [0.2.3] - 2026-07-02

### Changed
- Added explanatory comments throughout `server.py` and `openapi_utils.py`
  covering the non-obvious "why" behind auth handling, caching, schema
  recursion limits, and Swagger 2 vs OpenAPI 3 body-shape differences.
- Rewrote the README intro with a proper project description — what the
  server does, why it exists, and what it makes reachable — and corrected
  the Claude Desktop config example to match the actual `uv`-based launch
  command and `vcf-mcp` server name.

## [0.2.2] - 2026-07-02

### Fixed
- Added `pyproject.toml` + `uv.lock` — Claude Desktop launches this server via
  `uv --directory ... run server.py` (see `claude_desktop_config.json`), and
  with no project file `uv run` fell back to an environment with no
  dependencies installed, crashing on `import httpx` at startup. Now matches
  the same `uv`-project convention as the sibling `privateAI-demo` and
  `personalHRAssistant` servers.
- Added the missing `env` block for `vcf-mcp` in `claude_desktop_config.json`
  (it previously had none, so even a successful launch would have had no
  credentials configured).

## [0.2.1] - 2026-07-02

### Added
- `FLEET_VERIFY_SSL` / `VCFOPS_VERIFY_SSL` env vars (default `false`) to skip
  TLS certificate verification against self-signed lab VCF instances, mirroring
  `config.VCF_OPS_VERIFY_SSL` in `privateAI-demo`. Without this, `vcf-ops` calls
  failed with `CERTIFICATE_VERIFY_FAILED` against a real lab instance.

### Verified
- Live end-to-end test against a real lab: `fleet` (`getLcmHealthStatusV2UsingGET`)
  and `vcf-ops` (`getResources`) both returned `200` using the new credential-based
  auth flow.

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
