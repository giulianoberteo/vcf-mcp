"""
Optional HashiCorp Vault-backed secret retrieval for vcf-mcp.

Instead of putting VCF passwords directly in claude_desktop_config.json's
"env" block (plaintext on disk), this module lets the server fetch each
spec's password from Vault's KV v2 store at request time. What DOES still
need to live in claude_desktop_config.json is a Vault AppRole role_id +
secret_id pair — but that pair is scoped read-only to secret/vcf-mcp/*
(nothing else in the vault), short-lived (the token it exchanges for has a
1h TTL, 4h max), and revocable/rotatable independently of the actual VCF
credentials. Compromising it doesn't hand over VCF admin passwords directly
without also compromising Vault's audit-logged access.

This is additive, not a hard requirement: if VAULT_ADDR isn't set, callers
fall back to reading the plaintext env vars (FLEET_PASSWORD etc.) exactly as
before — see config.settings.get_password().

Environment variables:
  VAULT_ADDR       e.g. http://127.0.0.1:8200 (dev) or https://vault.example.com
  VAULT_ROLE_ID    AppRole role_id for the "vcf-mcp" role
  VAULT_SECRET_ID  AppRole secret_id for the "vcf-mcp" role
  VAULT_KV_MOUNT   optional, default "secret" (the KV v2 mount point)
  VAULT_KV_PREFIX  optional, default "vcf-mcp" (secrets live under <mount>/<prefix>/<spec>)

One-time setup (dev mode): see config/vault_setup.py.
"""
import os
from typing import Optional

import hvac

_client: Optional[hvac.Client] = None


def is_configured() -> bool:
    """True if enough env vars are present to attempt Vault auth at all."""
    return bool(os.environ.get("VAULT_ADDR") and os.environ.get("VAULT_ROLE_ID") and os.environ.get("VAULT_SECRET_ID"))


def _get_client() -> hvac.Client:
    """Authenticate to Vault via AppRole once, then reuse the client (and its
    token) for the rest of the process — same acquire-once-and-cache pattern
    as the VCF OpsToken/bearer-token flows in server.py."""
    global _client
    if _client is not None and _client.is_authenticated():
        return _client

    client = hvac.Client(url=os.environ["VAULT_ADDR"])
    client.auth.approle.login(
        role_id=os.environ["VAULT_ROLE_ID"],
        secret_id=os.environ["VAULT_SECRET_ID"],
    )
    _client = client
    return client


def get_password(spec_name: str) -> str:
    """Read the password for a spec from Vault KV v2 at
    <VAULT_KV_MOUNT>/<VAULT_KV_PREFIX>/<spec_name>, key "password"."""
    mount = os.environ.get("VAULT_KV_MOUNT", "secret")
    prefix = os.environ.get("VAULT_KV_PREFIX", "vcf-mcp")
    client = _get_client()
    resp = client.secrets.kv.v2.read_secret_version(
        mount_point=mount,
        path=f"{prefix}/{spec_name}",
        raise_on_deleted_version=True,
    )
    return resp["data"]["data"]["password"]
