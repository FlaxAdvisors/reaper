"""Every site vault carries the install-file secrets the templates reference (operator
2026-11-10: rendered at every bang; values copied between site vaults under the
install-file cross-site exception). Decrypted in memory only."""
import os
import re
import subprocess

import pytest
import yaml

_REAPER = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
ROLE = os.path.join(_REAPER, "roles", "apply_pxe_payloads")
SITES = ("lavergne", "eindhoven", "braintree")


class _Loader(yaml.SafeLoader):
    pass


_Loader.add_constructor("!vault", lambda loader, node: "<nested vault>")


def _referenced() -> set:
    names = set()
    for sub in ("templates/install", "files/install-kept"):
        for dp, _, fn in os.walk(os.path.join(ROLE, sub)):
            for f in fn:
                names |= set(re.findall(r"vault_install_secrets\.([a-z0-9_]+)", open(os.path.join(dp, f)).read()))
    return names


def _vault(site: str) -> "tuple[str, dict]":
    p = os.path.join(_REAPER, "inventories", site, "group_vars", "all", "vault.yml")
    if not os.path.exists(os.path.expanduser(f"~/.vault_pass_{site}")):
        pytest.skip(f"no vault password for {site} on this host")
    out = subprocess.run(["ansible-vault", "view", "--vault-password-file",
                          os.path.join(_REAPER, "scripts", "vault-password.sh"), p],
                         capture_output=True, text=True, check=True, cwd=_REAPER,
                         env={**os.environ, "REAPER_SITE": site}).stdout
    return open(p).read(), yaml.load(out, Loader=_Loader)


@pytest.mark.parametrize("site", SITES)
def test_vault_defines_every_install_secret(site):
    raw, v = _vault(site)
    assert raw.startswith("$ANSIBLE_VAULT")
    assert _referenced() <= set(v.get("vault_install_secrets") or {})
    keys = v.get("vault_install_authorized_keys") or []
    assert len(keys) >= 8 and all(re.match(r"^(ssh-(rsa|ed25519)|ecdsa-\S+) \S{20,}", k) for k in keys)


def test_install_values_identical_across_sites():
    vs = [_vault(s)[1] for s in SITES]
    assert all(v.get("vault_install_secrets") == vs[0].get("vault_install_secrets") for v in vs)
    assert all(v.get("vault_install_authorized_keys") == vs[0].get("vault_install_authorized_keys") for v in vs)
