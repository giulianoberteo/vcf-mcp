# vcf-mcp

Talk to your VMware Cloud Foundation lab or environment from Claude — ask
about resource health, pull active alerts, manage certificates, or drive
lifecycle operations — without hand-writing a single API integration.

`vcf-mcp` is a Python [MCP](https://modelcontextprotocol.io) server that
exposes three VCF REST APIs to any MCP-compatible client (Claude Desktop,
Claude Code, etc.) by reading their OpenAPI/Swagger specs directly, rather
than shipping a hand-coded wrapper per endpoint:

- **`fleet`** — VCF Operations Fleet Management API (Swagger 2.0, 106 operations)
- **`vcf-ops`** — VCF Operations API (OpenAPI 3.0, 370 operations)
- **`sddc`** — VCF (SDDC Manager) API (OpenAPI 3.0, 375 operations)

Across the three specs that's 851 operations covering nearly everything you'd
otherwise do through the VCF UI — resource and alert management, certificate
operations, LCM/environment lifecycle, domain/workload management, auth
administration, and more — all reachable through natural-language requests.

All three spec files ship inside `specs/` and are parsed and normalized at
startup into one common shape, so the server logic doesn't care which spec
format an operation came from — see `openapi_utils.py` if you're curious how
that normalization works.

## How it works

Instead of 851 individual MCP tools, this server exposes 4:

| Tool | Purpose |
|---|---|
| `list_specs()` | Shows all specs, endpoint counts, and whether credentials are configured |
| `search_endpoints(spec, query)` | Keyword search over operation_id / path / summary / tags |
| `get_endpoint(spec, operation_id)` | Full parameter list + resolved request body JSON schema for one operation |
| `call_api(spec, operation_id, path_params, query_params, body, extra_headers)` | Looks up the operation, substitutes path params into the URL, attaches query params and JSON body, adds the `Authorization` header, and executes the HTTP call |

The typical flow a model follows: `search_endpoints` → `get_endpoint` → `call_api`.

## How a spec file becomes a working API call

There's no generated code and no per-endpoint wrapper anywhere in this
repo — everything the server needs to find an endpoint and call it comes
from parsing the spec file itself, at startup, in memory. Three steps:

**1. Normalize the spec once, at first use.** `openapi_utils.load_and_normalize_spec`
detects whether a spec is Swagger 2.0 (`fleet`) or OpenAPI 3.0 (`vcf-ops`,
`sddc`) by checking for a `swagger` vs `openapi` key, then converts either
shape into one common list of operations:

```python
{
    "operation_id": "getResources",
    "method": "GET",
    "path": "/api/resources/{id}",
    "summary": "...",
    "tags": [...],
    "parameters": [{"name": ..., "in": "path"|"query"|"header", "required": ..., "type": ..., "description": ...}],
    "request_body_schema": {...} | None,   # $refs already resolved into a real JSON schema
}
```

The two formats disagree about where the request body lives (Swagger 2
mixes it into `parameters` with `in: "body"`; OpenAPI 3 has its own
`requestBody.content["application/json"].schema`) and about `$ref` resolution
(`#/definitions/Foo` vs `#/components/schemas/Foo`) — `_resolve_schema`
walks either one, inlining refs recursively (cycle-safe, capped at 6 levels
deep) so `request_body_schema` always comes out as a ready-to-read JSON
schema, regardless of which spec it came from. The result is cached in
memory (`server._cache`) so a ~370-operation spec is only parsed once per
process, not once per tool call.

**2. Find the right operation by keyword, not by knowing the API.** A model
doesn't need to already know an operation's exact name — `search_endpoints`
scores every operation by how many times the query string appears across
its `operation_id`, `path`, `summary`, and `tags`, and returns the best
matches. `get_endpoint` then hands back that operation's full parameter
list and resolved body schema, so the model knows exactly what `call_api`
needs before calling it.

**3. Build the HTTP request purely from what the spec said.** `call_api`
takes the same operation dict and assembles a real request with nothing
hardcoded per-endpoint:

- the URL is `base_url` (from your env config) + `server_prefix` (the
  API's own base path, e.g. `/suite-api`, read straight out of the spec's
  `basePath` or `servers[0].url`) + the operation's `path` template, with
  any `{placeholder}` in that template substituted from `path_params` —
  and it fails fast if a required one is missing or left unresolved;
- `query_params` are attached as-is, and `body` is only sent when the
  operation actually declares a `request_body_schema` or uses a method
  that expects one;
- the `Authorization` header is derived from the spec's configured
  credentials and auth scheme (see below) — never hand-typed per call.

None of this logic branches on *which* API it's talking to. Adding a fourth
VCF API means dropping its spec file into `specs/` and adding one entry to
`config.SPECS` — `search_endpoints`, `get_endpoint`, and `call_api` pick it
up automatically because they only ever operate on the normalized shape,
never on a spec's original format.

## Setup

With [uv](https://docs.astral.sh/uv/) (recommended — used by `claude_desktop_config.json` below):

```bash
cd vcf-mcp
uv sync
cp .env.example .env   # then fill in real values
```

Or with plain pip:

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
- `SDDC_BASE_URL`, `SDDC_USER`, `SDDC_PASSWORD` — for the SDDC Manager API
- `FLEET_VERIFY_SSL` / `VCFOPS_VERIFY_SSL` / `SDDC_VERIFY_SSL` (optional, default
  `false`) — set to `true` to enforce TLS certificate verification; defaults to
  skipping it since lab VCF instances typically run self-signed certs
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
- **`sddc`** — exchanges the username/password for a bearer access token via
  `POST /v1/tokens`, then caches it in memory the same way as `vcf-ops`.

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
    "vcf-mcp": {
      "command": "uv",
      "args": [
        "--directory", "/absolute/path/to/vcf-mcp",
        "run", "/absolute/path/to/vcf-mcp/server.py"
      ],
      "env": {
        "FLEET_BASE_URL": "https://your-fleet-management-host",
        "FLEET_USER": "admin@local",
        "FLEET_PASSWORD": "your-fleet-password",
        "VCFOPS_BASE_URL": "https://your-vcf-ops-host",
        "VCFOPS_USER": "your-vcf-ops-username",
        "VCFOPS_PASSWORD": "your-vcf-ops-password",
        "SDDC_BASE_URL": "https://your-sddc-manager-host",
        "SDDC_USER": "administrator@vsphere.local",
        "SDDC_PASSWORD": "your-sddc-password"
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
