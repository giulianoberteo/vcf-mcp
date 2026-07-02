# Changelog

All notable changes to this project are documented in this file.

## [0.1.0] - 2026-07-02

### Added
- Initial git repository setup.
- `.gitignore` (excludes venv, `__pycache__`, `.env`).
- `.env.example` documenting required environment variables (`FLEET_BASE_URL`, `FLEET_API_TOKEN`, `VCFOPS_BASE_URL`, `VCFOPS_API_TOKEN`, `API_TIMEOUT_SECONDS`).

### Fixed
- Moved `vcf-ops-public-api.json` and `vcf-fleet-management-api-docs.json` into `specs/` (renaming the latter to `fleet-management-api-docs.json`) to match the paths `server.py` expects — previously the server would fail to find either spec file at startup.
- Corrected `cd vcf-mcp-server` / example config path in README.md to the actual folder name `vcf-mcp`.
