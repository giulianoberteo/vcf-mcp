"""
One-time (or repeatable) setup script that provisions everything vcf-mcp
needs in Vault: a read-only policy scoped to secret/vcf-mcp/*, an AppRole
bound to that policy, and the three VCF passwords themselves — read from
your existing .env so you don't have to retype them.

This talks to Vault with a privileged token (e.g. the dev-mode root token,
or an operator token with policy/auth-method-admin rights in a real Vault) —
that token is NOT what ends up in claude_desktop_config.json. This script's
whole purpose is to trade a one-time privileged setup for a narrow,
long-lived (role_id, secret_id) pair that's all the running server ever
needs to hold.

Usage:
    export VAULT_ADDR=http://127.0.0.1:8200
    export VAULT_TOKEN=<a token with permission to write policies/auth methods>
    uv run python -m config.vault_setup

Prints the AppRole role_id and secret_id at the end — put those (plus
VAULT_ADDR) into claude_desktop_config.json's env block INSTEAD OF the
FLEET_PASSWORD/VCFOPS_PASSWORD/SDDC_PASSWORD entries.
"""
import os
import sys
from pathlib import Path

import hvac

from config.settings import SPECS

POLICY_NAME = "vcf-mcp-read"
ROLE_NAME = "vcf-mcp"
KV_MOUNT = os.environ.get("VAULT_KV_MOUNT", "secret")
KV_PREFIX = os.environ.get("VAULT_KV_PREFIX", "vcf-mcp")

POLICY_HCL = f"""
path "{KV_MOUNT}/data/{KV_PREFIX}/*" {{
  capabilities = ["read"]
}}
path "{KV_MOUNT}/metadata/{KV_PREFIX}/*" {{
  capabilities = ["list", "read"]
}}
"""


def _read_dotenv_passwords() -> dict[str, str]:
    """Pull each spec's *_PASSWORD value straight out of .env, so this
    script doesn't require re-typing credentials that are already there."""
    env_path = Path(__file__).parent.parent / ".env"
    values: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()

    passwords = {}
    for spec_name, cfg in SPECS.items():
        pw = values.get(cfg["password_env"], "")
        if not pw:
            print(f"WARNING: no {cfg['password_env']} found in .env — skipping '{spec_name}'", file=sys.stderr)
            continue
        passwords[spec_name] = pw
    return passwords


def main() -> None:
    vault_addr = os.environ.get("VAULT_ADDR")
    vault_token = os.environ.get("VAULT_TOKEN")
    if not vault_addr or not vault_token:
        print("Set VAULT_ADDR and VAULT_TOKEN (a privileged setup token) before running this.", file=sys.stderr)
        sys.exit(1)

    client = hvac.Client(url=vault_addr, token=vault_token)
    if not client.is_authenticated():
        print("Could not authenticate to Vault with VAULT_TOKEN.", file=sys.stderr)
        sys.exit(1)

    # Make sure the KV v2 engine is mounted at KV_MOUNT (harmless if it already is).
    try:
        client.sys.enable_secrets_engine(backend_type="kv", path=KV_MOUNT, options={"version": "2"})
    except hvac.exceptions.InvalidRequest:
        pass  # already mounted

    client.sys.create_or_update_policy(name=POLICY_NAME, policy=POLICY_HCL)
    print(f"Policy '{POLICY_NAME}' written.")

    try:
        client.sys.enable_auth_method("approle")
    except hvac.exceptions.InvalidRequest:
        pass  # already enabled

    client.auth.approle.create_or_update_approle(
        role_name=ROLE_NAME,
        token_policies=[POLICY_NAME],
        token_ttl="1h",
        token_max_ttl="4h",
    )
    print(f"AppRole '{ROLE_NAME}' bound to policy '{POLICY_NAME}'.")

    passwords = _read_dotenv_passwords()
    for spec_name, pw in passwords.items():
        client.secrets.kv.v2.create_or_update_secret(
            mount_point=KV_MOUNT,
            path=f"{KV_PREFIX}/{spec_name}",
            secret={"password": pw},
        )
        print(f"Wrote password for '{spec_name}' to {KV_MOUNT}/{KV_PREFIX}/{spec_name}.")

    role_id = client.auth.approle.read_role_id(ROLE_NAME)["data"]["role_id"]
    secret_id = client.auth.approle.generate_secret_id(ROLE_NAME)["data"]["secret_id"]

    print()
    print("Add these to claude_desktop_config.json's env block, REMOVING the")
    print("*_PASSWORD entries entirely:")
    print()
    print(f'  "VAULT_ADDR": "{vault_addr}",')
    print(f'  "VAULT_ROLE_ID": "{role_id}",')
    print(f'  "VAULT_SECRET_ID": "{secret_id}"')


if __name__ == "__main__":
    main()
