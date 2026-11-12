"""Part 3 R8: eindhoven's bmc-firmware-versions.json is reaper-managed.

The target (target_version + flash.artifact) is moved per site by `reaper fw <site>
bmc ...` (spec 2026-11-11), so it is not pinned here: the rendered file must carry
the inventory's entry unchanged, and the fields `reaper fw` never touches (match,
check, flash method, auto, the Part 3 post_flash_net_reset) are pinned."""
import json, pathlib, yaml, jinja2

_REAPER = pathlib.Path(__file__).resolve().parents[1]


def _var():
    v = yaml.safe_load(open(_REAPER / "inventories/eindhoven/group_vars/all/vars.yml"))
    return v["bmc_fw_platforms"]


def _render():
    tpl = (_REAPER / "roles/apply_bmc_fw_manifest/templates/bmc-firmware-versions.json.j2").read_text()
    env = jinja2.Environment()
    env.filters["to_nice_json"] = lambda o: json.dumps(o, indent=4, sort_keys=True)
    return json.loads(env.from_string(tpl).render(bmc_fw_platforms=_var()))


def test_eindhoven_manifest_renders_the_inventory_entry_plus_net_reset():
    m = _render()
    assert m == _var()
    e = m["tiogapass-openbmc"]
    assert isinstance(e["target_version"], str) and e["target_version"]
    assert e["flash"]["method"] == "redfish-update-multipart"
    assert e["flash"]["artifact"].startswith("SXC-Lab_OCP-Updates/OpenBMC/")
    assert e["flash"]["artifact"].endswith(".tar")
    assert set(e["flash"]) == {"method", "artifact"}
    assert e["check"] == {"method": "redfish-firmware-inventory", "id": "bmc_active"}
    assert e["match"]["product_name"] == ["(?i)tioga", "(?i)tp[-_ ]?", "(?i)wiwynn.*tp", "(?i)quanta.*tioga"]
    assert e["match"]["bmc_kind"] == "openbmc"
    assert e["auto"] is True
    assert e["post_flash_net_reset"] is True
    # Quanta Leopards are report-only (operator 2026-10-27, af75d7d): no flash entry.
    assert set(m) == {"tiogapass-openbmc", "leopard-ami"}
    assert m["leopard-ami"]["updatable"] is False and "flash" not in m["leopard-ami"]
