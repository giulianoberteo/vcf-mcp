"""
MCP server that dynamically constructs and executes API calls against three
VMware Cloud Foundation API specs:

  - "fleet"    : VCF Operations Fleet Management API (Swagger 2.0)
  - "vcf-ops"  : VCF Operations API                   (OpenAPI 3.0)
  - "sddc"     : VCF (SDDC Manager) API               (OpenAPI 3.0)

Rather than generating one MCP tool per endpoint (300+ of them), this server
exposes a small, fixed set of tools that let a model:

  1. search_endpoints  - find the right operation by keyword
  2. get_endpoint       - inspect its parameters / body schema in detail
  3. call_api           - dynamically build the HTTP request (method, URL,
                          path/query params, JSON body, auth header) from
                          the spec and execute it

No API tokens are stored anywhere — every spec authenticates from a
username/password pair supplied via environment variables, and any token
needed on the wire is derived at call time (and cached in memory only,
never written to disk):

  - "fleet" authenticates with HTTP Basic (base64 of user:password),
            built fresh from credentials on every call. See
            https://knowledge.broadcom.com/external/article/409715
  - "vcf-ops" authenticates by exchanging user/password for a short-lived
              OpsToken via POST /api/auth/token/acquire, then caching that
              token in memory for the life of the process.
  - "sddc"    authenticates by exchanging user/password for a bearer access
              token via POST /v1/tokens, then caching that token in memory
              for the life of the process (same acquire-once-and-cache
              pattern as vcf-ops, just a different endpoint/response shape).

See config/settings.py for the full list of environment variables each spec reads
(base URL, credentials, SSL verification) and how to add a new API spec.
"""
import base64
import os
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

from config.settings import SERVER_NAME, SPECS, TIMEOUT, verify_ssl
from config.openapi_utils import load_and_normalize_spec

# Parsed/normalized specs are expensive to build (vcf-ops alone is ~370
# operations with nested $ref schemas), so we only do it once per spec and
# keep the result around for the life of the process.
_cache: dict[str, dict] = {}

# Cached so we don't re-acquire a token on every call — vcf-ops OpsTokens are
# valid 6 hours, but sddc's are only valid 1 hour, so a long-running process
# WILL end up holding an expired token. We don't track expiry proactively;
# instead call_api treats a 401 as "cached token is stale" and clears it here
# so the next _build_auth_header() re-acquires. Keyed by spec name; never
# written to disk.
_token_cache: dict[str, str] = {}


def _acquire_token(spec_name: str, base_url: str, user: str, password: str) -> str:
    """Exchange credentials for a short-lived token via this spec's configured
    token_path, and cache it in memory. vcf-ops and sddc both work this way —
    POST username/password, pull the token back out of a named response field
    — just at different paths with different field names, both in config."""
    if spec_name in _token_cache:
        return _token_cache[spec_name]
    cfg = SPECS[spec_name]
    body: dict[str, str] = {"username": user, "password": password}
    # authSource is only relevant for LDAP-backed accounts — omit it entirely
    # for local users, or the API will reject the login. Only vcf-ops has
    # this concept.
    auth_source = os.environ.get(cfg.get("auth_source_env", ""), "")
    if auth_source:
        body["authSource"] = auth_source
    with httpx.Client(timeout=TIMEOUT, verify=verify_ssl(spec_name)) as client:
        resp = client.post(
            f"{base_url.rstrip('/')}{cfg['token_path']}",
            json=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        token = resp.json()[cfg["token_response_field"]]
    _token_cache[spec_name] = token
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
        return {"Authorization": f"{cfg['auth_scheme']} {encoded}"}

    if cfg["auth"] == "token_acquire":
        token = _acquire_token(spec_name, base_url, user, password)
        return {"Authorization": f"{cfg['auth_scheme']} {token}"}

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


mcp = FastMCP(SERVER_NAME)


@mcp.tool()
def list_specs() -> dict:
    """
    List the API specs available on this server ('fleet', 'vcf-ops', and 'sddc'), including
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
    Search for API operations within a spec ('fleet', 'vcf-ops', or 'sddc') by keyword.
    Matches against operation_id, path, summary, and tags. Use this first to find
    the operation_id you need, then call get_endpoint for full details before
    calling call_api.

    Args:
        spec: 'fleet', 'vcf-ops', or 'sddc'
        query: keyword(s) to search for, e.g. "certificate", "resources", "symptom"
        limit: max number of results to return (default 15)
    """
    norm = _get_spec(spec)
    q = query.lower().strip()

    # Score each operation by how many times the query appears across its
    # id/path/summary/tags, combined into one lowercase string ("haystack").
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
    scored.sort(key=lambda x: -x[0])  # highest match count first

    # Trim to `limit` and return just enough to pick the right one before
    # calling get_endpoint() for full details.
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
        spec: 'fleet', 'vcf-ops', or 'sddc'
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
        spec: 'fleet', 'vcf-ops', or 'sddc'
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

    request_kwargs: dict[str, Any] = {"params": query_params}
    # Only attach a JSON body for operations that actually accept one —
    # sending a stray body on a GET has caused rejected requests before.
    if op["request_body_schema"] is not None or op["method"] in ("POST", "PUT", "PATCH"):
        if body:
            request_kwargs["json"] = body

    # A cached token can go stale mid-process (sddc's are only valid 1 hour)
    # without us knowing in advance, so if the server rejects it we clear the
    # cache and retry once with a freshly acquired token before giving up.
    with httpx.Client(timeout=TIMEOUT, verify=verify_ssl(spec)) as client:
        for attempt in range(2):
            headers = {"Accept": "application/json"}
            headers.update(_build_auth_header(spec, base_url))
            headers.update(extra_headers)  # let the caller override anything above if truly needed
            resp = client.request(op["method"], url, headers=headers, **request_kwargs)
            if resp.status_code != 401 or attempt == 1:
                break
            _token_cache.pop(spec, None)

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
