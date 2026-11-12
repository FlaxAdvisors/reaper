"""Lavergne's vault carries every flax-stack secret eindhoven has, as NEW values.

Compares key names, value lengths and sha256 digests only -- never prints or
asserts on a value (RULE ZERO). Needs ~/.vault_pass_<site> for both sites;
skips when a site's vault password is unavailable.
"""
import hashlib
import os
import subprocess

import pytest
import yaml

_REAPER = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
LVQ_VAULT = os.path.join(_REAPER, "inventories", "lavergne", "group_vars", "all", "vault.yml")
_CACHE = {}


class _Loader(yaml.SafeLoader):
    """Inline `!vault` scalars (nested encrypted values) load as opaque strings."""


_Loader.add_constructor("!vault", lambda loader, node: loader.construct_scalar(node))


def _vault(site):
    if site not in _CACHE:
        path = os.path.join(_REAPER, "inventories", site, "group_vars", "all", "vault.yml")
        env = dict(os.environ, REAPER_SITE=site)
        r = subprocess.run(
            ["ansible-vault", "view", "--vault-password-file",
             os.path.join(_REAPER, "scripts", "vault-password.sh"), path],
            capture_output=True, text=True, env=env, cwd=_REAPER)
        if r.returncode != 0:
            pytest.skip("cannot decrypt %s vault" % site)
        _CACHE[site] = yaml.load(r.stdout, Loader=_Loader) or {}
    return _CACHE[site]


def _sha(site, key):
    return hashlib.sha256(str(_vault(site)[key]).encode()).hexdigest()


def test_lavergne_has_every_stack_key():
    needed = {"bang_vrrp_auth_pass", "vault_flax_stack_postgres_superuser_password",
              "vault_flax_stack_db_passwords"}
    assert needed <= set(_vault("lavergne"))


def test_db_password_services_match_eindhoven():
    assert set(_vault("lavergne")["vault_flax_stack_db_passwords"]) == \
        set(_vault("eindhoven")["vault_flax_stack_db_passwords"])


def test_vrrp_pass_is_8_chars():
    assert len(str(_vault("lavergne")["bang_vrrp_auth_pass"])) == 8


def test_lavergne_secrets_are_not_eindhoven_copies():
    for k in ("bang_vrrp_auth_pass", "vault_flax_stack_postgres_superuser_password"):
        assert _sha("lavergne", k) != _sha("eindhoven", k), k
    lvq = _vault("lavergne")["vault_flax_stack_db_passwords"]
    ehv = _vault("eindhoven")["vault_flax_stack_db_passwords"]
    for svc in lvq:
        assert hashlib.sha256(str(lvq[svc]).encode()).digest() != \
            hashlib.sha256(str(ehv[svc]).encode()).digest(), svc


def test_vault_file_is_encrypted():
    with open(LVQ_VAULT, "rb") as f:
        assert f.read(14) == b"$ANSIBLE_VAULT"
