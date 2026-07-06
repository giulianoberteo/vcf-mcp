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

  FLEET_VERIFY_SSL     optional, default false (lab VCF instances typically
                       run self-signed certs); set "true" to verify
  VCFOPS_VERIFY_SSL    optional, default false; set "true" to verify
  SDDC_VERIFY_SSL      optional, default false; set "true" to verify

  API_TIMEOUT_SECONDS  optional, default 30
  MCP_SERVER_NAME      optional, default "vcf-mcp" — name the MCP client
                       sees for this server

  VAULT_ADDR, VAULT_ROLE_ID, VAULT_SECRET_ID — optional; if all three are
  set, passwords are read from HashiCorp Vault instead of the *_PASSWORD
  env vars above. See config/vault_client.py.

To add a new VCF API: drop its spec file in specs/, add one entry to SPECS
below, and add its base_url/user/password (and verify_ssl) env vars to
.env.example. Everything else — search, get_endpoint, call_api, auth — picks
it up automatically.
"""
import os
from pathlib import Path

from config import vault_client

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
}

TIMEOUT = float(os.environ.get("API_TIMEOUT_SECONDS", "30"))


def verify_ssl(spec_name: str) -> bool:
    """Defaults to False — lab VCF instances typically run self-signed certs.
    Set <SPEC>_VERIFY_SSL=true to enforce verification."""
    return os.environ.get(SPECS[spec_name]["verify_ssl_env"], "").strip().lower() == "true"


def get_password(spec_name: str) -> str:
    """Password for a spec, from Vault if configured, else the plaintext
    *_PASSWORD env var (the original behavior, kept as a fallback so this
    still works without Vault set up at all)."""
    if vault_client.is_configured():
        return vault_client.get_password(spec_name)
    return os.environ.get(SPECS[spec_name]["password_env"], "")
