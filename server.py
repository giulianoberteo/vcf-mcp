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

No API tokens are stored anywhere — both specs authenticate from a
username/password pair supplied via environment variables, and any token
needed on the wire is derived at call time (and cached in memory only,
never written to disk):

  - "fleet"   authenticates with HTTP Basic (base64 of user:password),
              built fresh from credentials on every call. See
              https://knowledge.broadcom.com/external/article/409715
  - "vcf-ops" authenticates by exchanging user/password for a short-lived
              OpsToken via POST /api/auth/token/acquire, then caching that
              token in memory for the life of the process (mirrors
              _acquire_ops_token in privateAI-demo/mcp/server.py).

Configuration (environment variables):
  FLEET_BASE_URL       e.g. https://fleet.example.com   (required to call fleet endpoints)
  FLEET_USER           Fleet Management username, e.g. admin@local
  FLEET_PASSWORD       Fleet Management password

  VCFOPS_BASE_URL      e.g. https://vcf-ops.example.com  (required to call vcf-ops endpoints)
  VCFOPS_USER           VCF Operations username
  VCFOPS_PASSWORD       VCF Operations password
  VCFOPS_AUTH_SOURCE    optional; auth source display name for LDAP users

  FLEET_VERIFY_SSL      optional, default false (lab VCF instances typically
                        run self-signed certs); set "true" to verify
  VCFOPS_VERIFY_SSL     optional, default false; set "true" to verify

  API_TIMEOUT_SECONDS   optional, default 30
"""
import base64
import os
from pathlib import Path
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

from openapi_utils import load_and_normalize_spec

SPEC_DIR = Path(__file__).parent / "specs"

# Everything the server needs to know about each API lives here — the spec
# file to parse, where to find its base URL/credentials, and which auth
# scheme to use. The two VCF products don't agree on how they want to be
# authenticated (see _build_auth_header below), so each entry says how.
SPECS = {
    "fleet": {
        "file": SPEC_DIR / "fleet-management-api-docs.json",
        "base_url_env": "FLEET_BASE_URL",
        "auth": "basic",
        "user_env": "FLEET_USER",
        "password_env": "FLEET_PASSWORD",
        "verify_ssl_env": "FLEET_VERIFY_SSL",
    },
    "vcf-ops": {
        "file": SPEC_DIR / "vcf-ops-public-api.json",
        "base_url_env": "VCFOPS_BASE_URL",
        "auth": "ops_token",
        "user_env": "VCFOPS_USER",
        "password_env": "VCFOPS_PASSWORD",
        "auth_source_env": "VCFOPS_AUTH_SOURCE",
        "verify_ssl_env": "VCFOPS_VERIFY_SSL",
    },
}


def _verify_ssl(spec_name: str) -> bool:
    """Defaults to False — lab VCF instances typically run self-signed certs.
    Set <SPEC>_VERIFY_SSL=true to enforce verification."""
    return os.environ.get(SPECS[spec_name]["verify_ssl_env"], "").strip().lower() == "true"

TIMEOUT = float(os.environ.get("API_TIMEOUT_SECONDS", "30"))

# Parsed/normalized specs are expensive to build (vcf-ops alone is ~370
# operations with nested $ref schemas), so we only do it once per spec and
# keep the result around for the life of the process.
_cache: dict[str, dict] = {}

# OpsTokens are valid for 6 hours and refresh on use, so there's no reason to
# re-acquire one on every call — we grab it once and hold it in memory for as
# long as the server runs. This is never written to disk.
_ops_token_cache: dict[str, str] = {}


def _acquire_ops_token(spec_name: str, base_url: str, user: str, password: str) -> str:
    """Exchange vcf-ops credentials for a short-lived OpsToken and cache it in memory."""
    if spec_name in _ops_token_cache:
        return _ops_token_cache[spec_name]
    cfg = SPECS[spec_name]
    # authSource is only relevant for LDAP-backed accounts — omit it entirely
    # for local users, or the API will reject the login.
    auth_source = os.environ.get(cfg["auth_source_env"], "")
    body: dict[str, str] = {"username": user, "password": password}
    if auth_source:
        body["authSource"] = auth_source
    with httpx.Client(timeout=TIMEOUT, verify=_verify_ssl(spec_name)) as client:
        resp = client.post(
            f"{base_url.rstrip('/')}/suite-api/api/auth/token/acquire",
            json=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        token = resp.json()["token"]
    _ops_token_cache[spec_name] = token
    return token


def _build_auth_header(spec_name: str, base_url: str) -> dict[str, str]:
    """Turn the configured username/password for a spec into an Authorization
    header, using whichever scheme that particular API actually expects."""
    cfg = SPECS[spec_name]
    user = os.environ.get(cfg["user_env"], "")
    password = os.environ.get(cfg["password_env"], "")
    if not user or not password:
        raise ValueError(
            f"{cfg['user_env']} and {cfg['password_env']} must both be set to call "
            f"the '{spec_name}' API."
        )

    if cfg["auth"] == "basic":
        # Fleet Management has no token-acquire endpoint of its own — it just
        # wants plain HTTP Basic on every request. See Broadcom KB 409715.
        encoded = base64.b64encode(f"{user}:{password}".encode()).decode()
        return {"Authorization": f"Basic {encoded}"}

    if cfg["auth"] == "ops_token":
        token = _acquire_ops_token(spec_name, base_url, user, password)
        return {"Authorization": f"OpsToken {token}"}

    raise ValueError(f"Unknown auth type '{cfg['auth']}' for spec '{spec_name}'")


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
        # The model is almost certainly guessing an operation_id instead of
        # having called search_endpoints first — point it back there.
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
    and credential environment variables are currently configured.
    """
    out = {}
    for name, cfg in SPECS.items():
        norm = _get_spec(name)
        out[name] = {
            "title": norm["title"],
            "version": norm["version"],
            "endpoint_count": len(norm["operations"]),
            "auth": cfg["auth"],
            "base_url_env": cfg["base_url_env"],
            "user_env": cfg["user_env"],
            "password_env": cfg["password_env"],
            "base_url_configured": bool(os.environ.get(cfg["base_url_env"])),
            "credentials_configured": bool(os.environ.get(cfg["user_env"])) and bool(os.environ.get(cfg["password_env"])),
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
        # Cheap relevance signal: how many times the query shows up across
        # id/path/summary/tags. Good enough for "find the right endpoint out
        # of a few hundred" — no need for real ranking here.
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

    # Fail loudly and early if a required {placeholder} wasn't supplied,
    # rather than sending a half-built URL and getting a confusing 404 back.
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

    # Belt and braces: catch any leftover {placeholder} the caller forgot
    # about, or a typo'd param name that never matched the template.
    if "{" in resolved_path:
        raise ValueError(
            f"Unresolved path placeholders remain in '{resolved_path}'. "
            f"Provide all required path_params for {operation_id}."
        )

    # server_prefix is the API's base path (e.g. "/suite-api") pulled from
    # the spec itself — the caller only has to worry about the host.
    url = base_url.rstrip("/") + norm["server_prefix"] + resolved_path

    headers = {"Accept": "application/json"}
    headers.update(_build_auth_header(spec, base_url))
    headers.update(extra_headers)  # let the caller override anything above if truly needed

    request_kwargs: dict[str, Any] = {
        "params": query_params,
        "headers": headers,
    }
    # Only attach a JSON body for operations that actually accept one —
    # sending a stray body on a GET has caused rejected requests before.
    if op["request_body_schema"] is not None or op["method"] in ("POST", "PUT", "PATCH"):
        if body:
            request_kwargs["json"] = body

    with httpx.Client(timeout=TIMEOUT, verify=_verify_ssl(spec)) as client:
        resp = client.request(op["method"], url, **request_kwargs)

    try:
        parsed = resp.json()
    except ValueError:
        # Not every endpoint returns JSON (e.g. plain-text or empty bodies) —
        # fall back to raw text rather than blowing up on a non-error response.
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
