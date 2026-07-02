# vcf-fleet-api MCP Server

A Python MCP server that dynamically constructs and executes API calls against
two VMware Cloud Foundation Operations specs, without pre-generating a tool
per endpoint:

- **`fleet`** — VCF Operations Fleet Management API (Swagger 2.0, 106 operations)
- **`vcf-ops`** — VCF Operations API (OpenAPI 3.0, 370 operations)

Both spec files ship inside `specs/` and are parsed and normalized at startup
into one common shape, so the server logic doesn't care which spec format an
operation came from.

## How it works

Instead of 476 individual MCP tools, this server exposes 4:

| Tool | Purpose |
|---|---|
| `list_specs()` | Shows both specs, endpoint counts, and whether credentials are configured |
| `search_endpoints(spec, query)` | Keyword search over operation_id / path / summary / tags |
| `get_endpoint(spec, operation_id)` | Full parameter list + resolved request body JSON schema for one operation |
| `call_api(spec, operation_id, path_params, query_params, body, extra_headers)` | Looks up the operation, substitutes path params into the URL, attaches query params and JSON body, adds the `Authorization` header, and executes the HTTP call |

The typical flow a model follows: `search_endpoints` → `get_endpoint` → `call_api`.

## Setup

```bash
cd vcf-mcp
python3 -m venv .venv && source .venv/bin/activate  # requires Python 3.10+
pip install -r requirements.txt
cp .env.example .env   # then fill in real values
```

Required environment variables (see `.env.example`):

- `FLEET_BASE_URL`, `FLEET_USER`, `FLEET_PASSWORD` — for the Fleet Management API
- `VCFOPS_BASE_URL`, `VCFOPS_USER`, `VCFOPS_PASSWORD` — for the VCF Operations API
- `VCFOPS_AUTH_SOURCE` (optional) — auth source display name, for LDAP `vcf-ops` users
- `FLEET_VERIFY_SSL` / `VCFOPS_VERIFY_SSL` (optional, default `false`) — set to
  `true` to enforce TLS certificate verification; defaults to skipping it since
  lab VCF instances typically run self-signed certs
- `API_TIMEOUT_SECONDS` (optional, default `30`)

No API token is ever stored in `.env` — only a username/password pair per
spec. `call_api` derives the `Authorization` header from those credentials
at request time, per each API's own auth scheme:

- **`fleet`** — HTTP Basic (`Authorization: Basic base64(user:password)`),
  rebuilt from credentials on every call. See
  [Broadcom KB 409715](https://knowledge.broadcom.com/external/article/409715/how-to-authorize-vcf-operations-fleet-ma.html).
- **`vcf-ops`** — exchanges the username/password for a short-lived OpsToken
  via `POST /api/auth/token/acquire`, then caches that token **in memory
  only** (never written to disk) for the life of the process. Mirrors
  `_acquire_ops_token` in `privateAI-demo/mcp/server.py`.

## Running standalone

```bash
python server.py
```

This starts the server on stdio, ready to be connected to by an MCP client.

## Connecting from Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "vcf-fleet-api": {
      "command": "python",
      "args": ["/absolute/path/to/vcf-mcp/server.py"],
      "env": {
        "FLEET_BASE_URL": "https://your-fleet-management-host",
        "FLEET_USER": "admin@local",
        "FLEET_PASSWORD": "your-fleet-password",
        "VCFOPS_BASE_URL": "https://your-vcf-ops-host",
        "VCFOPS_USER": "your-vcf-ops-username",
        "VCFOPS_PASSWORD": "your-vcf-ops-password"
      }
    }
  }
}
```

## Example interaction

1. `search_endpoints(spec="vcf-ops", query="resources")` →
   finds `getResources`, `updateResource`, etc.
2. `get_endpoint(spec="vcf-ops", operation_id="getResources")` →
   shows its query parameters and any body schema.
3. `call_api(spec="vcf-ops", operation_id="getResources", query_params={"pageSize": 50})` →
   builds `GET {VCFOPS_BASE_URL}/suite-api/api/resources?pageSize=50` with the
   auth header attached, executes it, and returns `{status_code, url, method, ok, response}`.

## Known limitations

- **Multipart/file-upload endpoints**: one Fleet endpoint
  (`uploadContentUsingPOST`) uses `multipart/form-data`, which isn't
  supported by the generic JSON-body path in `call_api`. It would need a
  dedicated code path if you need it.
- **Local `$ref` resolution only**: schemas resolve internal `#/...` refs;
  there are no external file references in either spec, so this isn't a
  practical limitation here.
- **Deeply recursive schemas** are capped at 6 levels of `$ref` resolution
  to keep `get_endpoint` output readable; deeper refs show as
  `{"$ref_name": "TypeName"}` placeholders instead of fully expanding.
