"""Render the deployed integra.json.j2 template and load it through the real
role_registry loader -- guards against template drift silently breaking the
deployed integra role (phase 6, spec 2026-07-05-flax-phase6-integra).

We render the actual Ansible template with the eindhoven inventory vars
(flax_role_integra_ports / flax_role_integra_vid), supplying a ``to_json``
filter (Ansible provides this at runtime; jinja2 core does not), then feed the
output through load_role_dir/validate_roles -- never a hand-built def -- so the
private _ports index and resolution path are exercised exactly as in production.
"""
import json
import os

import jinja2
import pytest

from flax_classify.role_registry import (
    load_role_dir, resolve_role, validate_roles)

_TEMPLATE = os.path.join(
    os.path.dirname(__file__), os.pardir,
    "roles", "apply_lease_agent", "templates", "roles.d", "integra.json.j2")

# The eindhoven-pinned vars (inventories/eindhoven/group_vars/all/vars.yml).
EINDHOVEN_VARS = {
    "flax_role_integra_ports": {"rabbit-gouda": ["et32/1"]},
    "flax_role_integra_vid": 31,
}


def _render(vars_):
    with open(_TEMPLATE) as fh:
        src = fh.read()
    env = jinja2.Environment(keep_trailing_newline=True)
    # Ansible ships a to_json filter; jinja2 core does not. Mirror it.
    env.filters.setdefault("to_json", lambda o, **kw: json.dumps(o))
    return env.from_string(src).render(**vars_)


@pytest.fixture
def integra_defs(tmp_path):
    rendered = _render(EINDHOVEN_VARS)
    # Must be valid JSON standalone (the loader is strict).
    parsed = json.loads(rendered)
    d = tmp_path / "roles.d"
    d.mkdir()
    (d / "integra.json").write_text(rendered)
    defs = load_role_dir(str(d))
    validate_roles(defs)
    return defs, parsed


def test_rendered_integra_is_valid_json(integra_defs):
    _, parsed = integra_defs
    assert parsed["role"] == "integra"


def test_universe_ports_as_written(integra_defs):
    _, parsed = integra_defs
    # Pre-normalisation, as written in the deployed file.
    assert parsed["universe"]["ports"] == {"rabbit-gouda": ["et32/1"]}


def test_capabilities_all_false(integra_defs):
    _, parsed = integra_defs
    caps = parsed["capabilities"]
    assert caps == {"reconcile_switch": False, "dhcp_reservations": False,
                    "firmware": False}
    assert all(v is False for v in caps.values())


def test_policy_vid_default(integra_defs):
    _, parsed = integra_defs
    assert parsed["policy"]["vid"]["default"] == 31


def test_record_keys(integra_defs):
    _, parsed = integra_defs
    assert parsed["record_keys"] == ["customer", "order"]


def test_loader_resolves_integra_port(integra_defs):
    defs, _ = integra_defs
    assert "integra" in defs
    assert resolve_role(defs, "rabbit-gouda", "et32/1") == "integra"


def test_vid_default_falls_back_when_unset():
    # Template default(31) must hold when the var is absent.
    rendered = _render({"flax_role_integra_ports": {"rabbit-gouda": ["et32/1"]}})
    parsed = json.loads(rendered)
    assert parsed["policy"]["vid"]["default"] == 31


def test_ports_default_falls_back_when_unset():
    rendered = _render({"flax_role_integra_vid": 31})
    parsed = json.loads(rendered)
    assert parsed["universe"]["ports"] == {"rabbit-gouda": ["et32/1"]}
