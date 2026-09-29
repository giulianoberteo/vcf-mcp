"""
Central configuration for the vcf-mcp server.

This is the one place that knows which VCF APIs exist, where their spec
files live on disk, which environment variables hold each one's base URL /
credentials / SSL setting, and the shared HTTP timeout. Kept separate from
server.py so wiring in a new VCF API (a new spec file, a new set of env
vars) never requires touching the request-building or MCP tool logic.

Environment variables (see .env.example):
  FLEET_BASE_URL       e.g. https://fleet.example.com   (required to call fleet endpoints)
  FLEET_USER           Fleet Management username, e.g. admin@local
  FLEET_PASSWORD       Fleet Management password

  VCFOPS_BASE_URL      e.g. https://vcf-ops.example.com  (required to call vcf-ops endpoints)
  VCFOPS_USER          VCF Operations username
  VCFOPS_PASSWORD      VCF Operations password
  VCFOPS_AUTH_SOURCE   optional; auth source display name for LDAP users

  SDDC_BASE_URL        e.g. https://sddc-manager.example.com (required to call sddc endpoints)
  SDDC_USER            SDDC Manager username, e.g. administrator@vsphere.local
  SDDC_PASSWORD        SDDC Manager password

  VCENTER_BASE_URL     e.g. https://vcenter.example.com (required to call
                       vcenter/vi-json endpoints; both specs hit the same
                       vCenter appliance and share one set of credentials)
  VCENTER_USER         vCenter username, e.g. administrator@vsphere.local
  VCENTER_PASSWORD     vCenter password

  FLEET_VERIFY_SSL     optional, default false (lab VCF instances typically
                       run self-signed certs); set "true" to verify
  VCFOPS_VERIFY_SSL    optional, default false; set "true" to verify
  SDDC_VERIFY_SSL      optional, default false; set "true" to verify
  VCENTER_VERIFY_SSL   optional, default false; set "true" to verify

  API_TIMEOUT_SECONDS  optional, default 30
  MCP_SERVER_NAME      optional, default "vcf-mcp" — name the MCP client
                       sees for this server

To add a new VCF API: drop its spec file in specs/, add one entry to SPECS
below, and add its base_url/user/password (and verify_ssl) env vars to
.env.example. Everything else — search, get_endpoint, call_api, auth — picks
it up automatically.
"""
import os
from pathlib import Path

SPEC_DIR = Path(__file__).parent.parent / "specs"

SERVER_NAME = os.environ.get("MCP_SERVER_NAME", "vcf-mcp")

# Everything the server needs to know about each API lives here — the spec
# file to parse, where to find its base URL/credentials, and which auth
# scheme to use. None of these three VCF products agree on how they want to
# be authenticated (see server._build_auth_header), so each entry says how.
SPECS = {
    "fleet": {
        "file": SPEC_DIR / "fleet-management-api-docs.json",
        "base_url_env": "FLEET_BASE_URL",
        "auth": "basic",
        "auth_scheme": "Basic",
        "user_env": "FLEET_USER",
        "password_env": "FLEET_PASSWORD",
        "verify_ssl_env": "FLEET_VERIFY_SSL",
    },
    "vcf-ops": {
        "file": SPEC_DIR / "vcf-ops-public-api.json",
        "base_url_env": "VCFOPS_BASE_URL",
        "auth": "token_acquire",
        "auth_scheme": "OpsToken",
        "user_env": "VCFOPS_USER",
        "password_env": "VCFOPS_PASSWORD",
        "auth_source_env": "VCFOPS_AUTH_SOURCE",
        "verify_ssl_env": "VCFOPS_VERIFY_SSL",
        "token_path": "/suite-api/api/auth/token/acquire",
        "token_response_field": "token",
    },
    "sddc": {
        "file": SPEC_DIR / "vmware-cloud-foundation.json",
        "base_url_env": "SDDC_BASE_URL",
        "auth": "token_acquire",
        "auth_scheme": "Bearer",
        "user_env": "SDDC_USER",
        "password_env": "SDDC_PASSWORD",
        "verify_ssl_env": "SDDC_VERIFY_SSL",
        "token_path": "/v1/tokens",
        "token_response_field": "accessToken",
    },
    # vCenter's REST session API (POST /api/session) takes HTTP Basic on the
    # login call itself (no JSON body) and hands back the session ID as a
    # bare JSON string — not a named field like vcf-ops/sddc — then wants
    # that ID echoed back on every call as a custom header, not Authorization.
    # See server._acquire_vcenter_session_token / _build_auth_header.
    "vcenter": {
        "file": SPEC_DIR / "vcenter.yaml",
        "base_url_env": "VCENTER_BASE_URL",
        "auth": "vcenter_session",
        "session_header": "vmware-api-session-id",
        "user_env": "VCENTER_USER",
        "password_env": "VCENTER_PASSWORD",
        "verify_ssl_env": "VCENTER_VERIFY_SSL",
        "token_path": "/api/session",
    },
    # Same vCenter appliance, but the legacy VIM ("Virtual Infrastructure")
    # object model exposed as JSON instead of SOAP. Authenticates the same
    # way as 'vcenter' above (same session mechanism), just a much larger,
    # lower-level API surface — prefer 'vcenter' unless you specifically
    # need a VIM managed-object operation.
    "vi-json": {
        "file": SPEC_DIR / "vi-json.yaml",
        "base_url_env": "VCENTER_BASE_URL",
        "auth": "vcenter_session",
        "session_header": "vmware-api-session-id",
        "user_env": "VCENTER_USER",
        "password_env": "VCENTER_PASSWORD",
        "verify_ssl_env": "VCENTER_VERIFY_SSL",
        "token_path": "/api/session",
    },
}

TIMEOUT = float(os.environ.get("API_TIMEOUT_SECONDS", "30"))


def verify_ssl(spec_name: str) -> bool:
    """Defaults to False — lab VCF instances typically run self-signed certs.
    Set <SPEC>_VERIFY_SSL=true to enforce verification."""
    return os.environ.get(SPECS[spec_name]["verify_ssl_env"], "").strip().lower() == "true"
