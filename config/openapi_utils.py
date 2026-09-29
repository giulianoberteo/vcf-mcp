"""
Normalizes Swagger 2.0 and OpenAPI 3.0 specs into a single common
in-memory representation so the MCP server can treat both specs
(Fleet Management = Swagger 2.0, VCF Operations = OpenAPI 3.0)
the same way.

Common operation shape:
{
    "operation_id": str,
    "method": "GET"/"POST"/...,
    "path": "/api/resources/{id}",
    "summary": str,
    "tags": [str, ...],
    "parameters": [
        {"name": str, "in": "path"|"query"|"header", "required": bool,
         "type": str, "description": str}
    ],
    "request_body_schema": dict | None,   # resolved JSON schema, or None
}
"""
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml


def _resolve_ref(root: dict, ref: str) -> Any:
    """Resolve a local JSON $ref like '#/definitions/Foo' or '#/components/schemas/Foo'."""
    assert ref.startswith("#/"), f"Only local refs are supported, got {ref}"
    node = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        node = node[part]
    return node


def _resolve_schema(root: dict, schema: Any, seen: set | None = None, depth: int = 0, max_depth: int = 6) -> Any:
    """Recursively resolve $refs in a schema. Cycle-safe and depth-limited so
    the output stays a reasonably sized, usable JSON schema."""
    if schema is None:
        return None
    if seen is None:
        seen = set()

    if isinstance(schema, dict):
        if "$ref" in schema:
            ref = schema["$ref"]
            # Some VCF schemas reference themselves (a Resource can contain a
            # list of related Resources, etc.), so without this guard we'd
            # recurse forever. Past max_depth we just leave a named pointer
            # instead of expanding further — plenty for a model to work with.
            if ref in seen or depth >= max_depth:
                return {"$ref_name": ref.split("/")[-1]}
            target = _resolve_ref(root, ref)
            return _resolve_schema(root, target, seen | {ref}, depth + 1, max_depth)

        out = {}
        for key, value in schema.items():
            if key in ("properties",) and isinstance(value, dict):
                out[key] = {
                    k: _resolve_schema(root, v, seen, depth + 1, max_depth)
                    for k, v in value.items()
                }
            elif key in ("items", "additionalProperties", "schema") and isinstance(value, (dict, list)):
                out[key] = _resolve_schema(root, value, seen, depth + 1, max_depth)
            elif key in ("allOf", "oneOf", "anyOf") and isinstance(value, list):
                out[key] = [_resolve_schema(root, v, seen, depth + 1, max_depth) for v in value]
            else:
                out[key] = value
        return out

    if isinstance(schema, list):
        return [_resolve_schema(root, item, seen, depth + 1, max_depth) for item in schema]

    return schema


def _server_prefix(servers: list) -> str:
    """Pull the base path prefix (e.g. "/suite-api", "/api") out of an
    OpenAPI 3 `servers` entry, regardless of whether the url is a bare
    relative path ("/suite-api") or a full templated URL
    ("https://{host}/api", "https://{vcenter-host}/sdk/vim25/{release}").
    The env var base URL always supplies the actual host, so only the path
    portion (with any {var} placeholders filled from their declared
    defaults) matters here."""
    if not servers:
        return ""
    server = servers[0]
    url = server.get("url", "")
    for name, var in server.get("variables", {}).items():
        default = var.get("default", "")
        url = url.replace("{" + name + "}", str(default))

    path = urlsplit(url).path if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url) else url
    return path.rstrip("/")


def _normalize_swagger2(spec: dict) -> dict:
    """Fleet Management ships as Swagger 2.0, where the request body is just
    another entry in "parameters" with in: "body" — there's no separate
    requestBody concept like in OpenAPI 3. We pull it out here so downstream
    code only ever has to deal with one shape (see module docstring)."""
    operations = []
    for path, methods in spec.get("paths", {}).items():
        # Path-level parameters (shared across methods) sometimes appear here
        path_level_params = methods.get("parameters", [])
        for method, op in methods.items():
            if method.lower() not in ("get", "post", "put", "delete", "patch", "head", "options"):
                continue
            params = []
            request_body_schema = None
            for p in path_level_params + op.get("parameters", []):
                if p.get("in") == "body":
                    request_body_schema = _resolve_schema(spec, p.get("schema"))
                elif p.get("in") == "formData":
                    params.append({
                        "name": p.get("name"),
                        "in": "formData",
                        "required": p.get("required", False),
                        "type": p.get("type", "string"),
                        "description": p.get("description", ""),
                    })
                else:
                    params.append({
                        "name": p.get("name"),
                        "in": p.get("in"),
                        "required": p.get("required", False),
                        "type": p.get("type", "string"),
                        "description": p.get("description", ""),
                    })
            operations.append({
                "operation_id": op.get("operationId"),
                "method": method.upper(),
                "path": path,
                "summary": op.get("summary", ""),
                "tags": op.get("tags", []),
                "parameters": params,
                "request_body_schema": request_body_schema,
            })

    base_path = spec.get("basePath", "") or ""
    server_prefix = base_path if base_path not in ("", "/") else ""

    return {
        "title": spec.get("info", {}).get("title", ""),
        "version": spec.get("info", {}).get("version", ""),
        "server_prefix": server_prefix,
        "operations": operations,
        "by_operation_id": {op["operation_id"]: op for op in operations if op["operation_id"]},
    }


def _normalize_openapi3(spec: dict) -> dict:
    """VCF Operations ships as OpenAPI 3.0, where the body lives under its own
    requestBody.content["application/json"].schema instead of being mixed in
    with the other parameters. Same end result as the Swagger 2 branch above,
    just a different starting shape to unpack."""
    operations = []
    for path, methods in spec.get("paths", {}).items():
        path_level_params = methods.get("parameters", [])
        for method, op in methods.items():
            if method.lower() not in ("get", "post", "put", "delete", "patch", "head", "options"):
                continue
            params = []
            for p in path_level_params + op.get("parameters", []):
                schema = p.get("schema", {})
                params.append({
                    "name": p.get("name"),
                    "in": p.get("in"),
                    "required": p.get("required", False),
                    "type": schema.get("type", "string") if isinstance(schema, dict) else "string",
                    "description": p.get("description", ""),
                })

            request_body_schema = None
            rb = op.get("requestBody")
            if rb:
                content = rb.get("content", {})
                json_content = content.get("application/json") or next(iter(content.values()), None)
                if json_content:
                    request_body_schema = _resolve_schema(spec, json_content.get("schema"))

            operations.append({
                "operation_id": op.get("operationId"),
                "method": method.upper(),
                "path": path,
                "summary": op.get("summary", ""),
                "tags": op.get("tags", []),
                "parameters": params,
                "request_body_schema": request_body_schema,
            })

    server_prefix = _server_prefix(spec.get("servers", []))

    return {
        "title": spec.get("info", {}).get("title", ""),
        "version": spec.get("info", {}).get("version", ""),
        "server_prefix": server_prefix,
        "operations": operations,
        "by_operation_id": {op["operation_id"]: op for op in operations if op["operation_id"]},
    }


def load_and_normalize_spec(file_path: Path) -> dict:
    file_path = Path(file_path)
    text = file_path.read_text()
    if file_path.suffix.lower() in (".yaml", ".yml"):
        spec = yaml.safe_load(text)
    else:
        spec = json.loads(text)
    if "swagger" in spec:
        return _normalize_swagger2(spec)
    if "openapi" in spec:
        return _normalize_openapi3(spec)
    raise ValueError(f"Unrecognized spec format in {file_path}: no 'swagger' or 'openapi' key")
