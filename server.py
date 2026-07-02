"""
MCP server that dynamically constructs and executes API calls against two
VMware Cloud Foundation Operations API specs:

  - "fleet"    : VCF Operations Fleet Management API (Swagger 2.0)
  - "vcf-ops"  : VCF Operations API                   (OpenAPI 3.0)

Rather than generating one MCP tool per endpoint (300+ of them), this server
exposes a small, fixed set of tools that let a model:

  1. search_endpoints  - find the right operation by keyword
  2. get_endpoint       - inspect its parameters / body schema in detail
  3. call_api           - dynamically build the HTTP request (method, URL,
                          path/query params, JSON body, auth header) from
                          the spec and execute it

Configuration (environment variables):
  FLEET_BASE_URL     e.g. https://fleet.example.com        (required to call fleet endpoints)
  FLEET_API_TOKEN    Bearer/API token sent in the Authorization header
  VCFOPS_BASE_URL    e.g. https://vcf-ops.example.com       (required to call vcf-ops endpoints)
  VCFOPS_API_TOKEN   Bearer/API token sent in the Authorization header
  API_TIMEOUT_SECONDS  optional, default 30
"""
import os
from pathlib import Path
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

from openapi_utils import load_and_normalize_spec

SPEC_DIR = Path(__file__).parent / "specs"

SPECS = {
    "fleet": {
        "file": SPEC_DIR / "fleet-management-api-docs.json",
        "base_url_env": "FLEET_BASE_URL",
        "token_env": "FLEET_API_TOKEN",
    },
    "vcf-ops": {
        "file": SPEC_DIR / "vcf-ops-public-api.json",
        "base_url_env": "VCFOPS_BASE_URL",
        "token_env": "VCFOPS_API_TOKEN",
    },
}

TIMEOUT = float(os.environ.get("API_TIMEOUT_SECONDS", "30"))

_cache: dict[str, dict] = {}


def _get_spec(name: str) -> dict:
    if name not in SPECS:
        raise ValueError(f"Unknown spec '{name}'. Available specs: {list(SPECS)}")
    if name not in _cache:
        _cache[name] = load_and_normalize_spec(SPECS[name]["file"])
    return _cache[name]


def _get_operation(spec_name: str, operation_id: str) -> dict:
    norm = _get_spec(spec_name)
    op = norm["by_operation_id"].get(operation_id)
    if not op:
        raise ValueError(
            f"operation_id '{operation_id}' not found in spec '{spec_name}'. "
            f"Use search_endpoints('{spec_name}', ...) to find the correct operation_id."
        )
    return op


mcp = FastMCP("vcf-fleet-api")


@mcp.tool()
def list_specs() -> dict:
    """
    List the API specs available on this server ('fleet' and 'vcf-ops'), including
    title, version, how many operations each has, and whether the required base URL
    and API token environment variables are currently configured.
    """
    out = {}
    for name, cfg in SPECS.items():
        norm = _get_spec(name)
        out[name] = {
            "title": norm["title"],
            "version": norm["version"],
            "endpoint_count": len(norm["operations"]),
            "base_url_env": cfg["base_url_env"],
            "token_env": cfg["token_env"],
            "base_url_configured": bool(os.environ.get(cfg["base_url_env"])),
            "token_configured": bool(os.environ.get(cfg["token_env"])),
        }
    return out


@mcp.tool()
def search_endpoints(spec: str, query: str, limit: int = 15) -> list[dict]:
    """
    Search for API operations within a spec ('fleet' or 'vcf-ops') by keyword.
    Matches against operation_id, path, summary, and tags. Use this first to find
    the operation_id you need, then call get_endpoint for full details before
    calling call_api.

    Args:
        spec: 'fleet' or 'vcf-ops'
        query: keyword(s) to search for, e.g. "certificate", "resources", "symptom"
        limit: max number of results to return (default 15)
    """
    norm = _get_spec(spec)
    q = query.lower().strip()
    scored = []
    for op in norm["operations"]:
        haystack = " ".join([
            op["operation_id"] or "",
            op["path"],
            op.get("summary") or "",
            " ".join(op.get("tags") or []),
        ]).lower()
        if q in haystack:
            scored.append((haystack.count(q), op))
    scored.sort(key=lambda x: -x[0])

    return [
        {
            "operation_id": op["operation_id"],
            "method": op["method"],
            "path": op["path"],
            "summary": op.get("summary"),
            "tags": op.get("tags"),
        }
        for _, op in scored[:limit]
    ]


@mcp.tool()
def get_endpoint(spec: str, operation_id: str) -> dict:
    """
    Get full details for one API operation: HTTP method, path template, all
    parameters (path/query/header, with type/required/description), and the
    resolved JSON schema for the request body (if any). Use this to know
    exactly what to pass into call_api.

    Args:
        spec: 'fleet' or 'vcf-ops'
        operation_id: the operation_id, as returned by search_endpoints
    """
    return _get_operation(spec, operation_id)


@mcp.tool()
def call_api(
    spec: str,
    operation_id: str,
    path_params: Optional[dict[str, Any]] = None,
    query_params: Optional[dict[str, Any]] = None,
    body: Optional[dict[str, Any]] = None,
    extra_headers: Optional[dict[str, str]] = None,
) -> dict:
    """
    Dynamically construct and execute an HTTP call for a given operation.

    Looks up the operation's method/path/parameter definitions from the spec,
    substitutes path_params into the URL template, attaches query_params,
    sends body as JSON if the operation accepts a request body, and adds the
    Authorization header automatically from the configured API token env var.

    Args:
        spec: 'fleet' or 'vcf-ops'
        operation_id: the operation_id, as returned by search_endpoints
        path_params: values for any {placeholders} in the URL path, e.g. {"id": "abc-123"}
        query_params: query string parameters, e.g. {"pageSize": 100}
        body: JSON request body, for POST/PUT/PATCH operations that need one
        extra_headers: additional headers to merge in (rarely needed)

    Returns:
        dict with keys: status_code, url, method, ok, response (parsed JSON or raw text)
    """
    op = _get_operation(spec, operation_id)
    cfg = SPECS[spec]
    norm = _get_spec(spec)

    base_url = os.environ.get(cfg["base_url_env"])
    if not base_url:
        raise ValueError(
            f"Environment variable {cfg['base_url_env']} is not set. "
            f"It must be set to the base URL for the '{spec}' API (e.g. https://host)."
        )

    path_params = path_params or {}
    query_params = query_params or {}
    body = body or {}
    extra_headers = extra_headers or {}

    # Validate + substitute path parameters
    path_template = op["path"]
    missing = []
    for p in op["parameters"]:
        if p["in"] == "path" and p["required"] and p["name"] not in path_params:
            missing.append(p["name"])
    if missing:
        raise ValueError(
            f"Missing required path_params for {operation_id}: {missing}. "
            f"Call get_endpoint('{spec}', '{operation_id}') to see all parameters."
        )

    resolved_path = path_template
    for name, value in path_params.items():
        resolved_path = resolved_path.replace("{" + name + "}", str(value))

    if "{" in resolved_path:
        raise ValueError(
            f"Unresolved path placeholders remain in '{resolved_path}'. "
            f"Provide all required path_params for {operation_id}."
        )

    url = base_url.rstrip("/") + norm["server_prefix"] + resolved_path

    token = os.environ.get(cfg["token_env"])
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = token if token.lower().startswith(("bearer", "basic")) else f"Bearer {token}"
    headers.update(extra_headers)

    request_kwargs: dict[str, Any] = {
        "params": query_params,
        "headers": headers,
    }
    if op["request_body_schema"] is not None or op["method"] in ("POST", "PUT", "PATCH"):
        if body:
            request_kwargs["json"] = body

    with httpx.Client(timeout=TIMEOUT) as client:
        resp = client.request(op["method"], url, **request_kwargs)

    try:
        parsed = resp.json()
    except ValueError:
        parsed = resp.text

    return {
        "status_code": resp.status_code,
        "url": str(resp.url),
        "method": op["method"],
        "ok": resp.is_success,
        "response": parsed,
    }


if __name__ == "__main__":
    mcp.run()
