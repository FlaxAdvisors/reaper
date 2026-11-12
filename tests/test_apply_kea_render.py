# tests/test_apply_kea_render.py
"""Render tests for apply_kea Kea config templates (jinja2, no live Kea)."""
import json
import os
import re
import jinja2

# Derive the repo root from this file so the render tests run in any worktree
# (was a hardcoded .worktrees path that broke whenever the worktree moved).
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Minimal braintree-shaped vlans (vid 16,17 + a post vid) for rendering.
VLANS = [
    {"vid": 16, "family": "leopard", "phase": "triage", "parent": "eth0", "cidr": "172.16.0.0/16", "gateway_vip": "172.16.0.10"},
    {"vid": 17, "family": "tiogapass", "phase": "triage", "parent": "eth0", "cidr": "172.17.0.0/16", "gateway_vip": "172.17.0.10"},
    {"vid": 31, "family": "grandcanyon", "phase": "post", "parent": "eth0", "cidr": "172.31.0.0/16", "gateway_vip": "172.31.0.10"},
]
CTX = dict(
    vlans=VLANS,
    bang_mgmt_iface="mgmt", bang_lab_domain="brt.lab",
    apply_kea_pool_start=50, apply_kea_pool_end=200,
    apply_kea_lab_domain="brt.lab",
    apply_kea_mgmt_subnet_id=4, apply_kea_mgmt_cidr="192.168.88.0/24",
    apply_kea_mgmt_vip="192.168.88.10",
    apply_kea_onie_pool_start="192.168.88.100",
    apply_kea_onie_pool_end="192.168.88.150",
    mgmt_guest_pool={"start": "192.168.88.200", "end": "192.168.88.249", "lease": "8h"},
    # v6 bootfile-url (option 59) — master's own per-vlan static, no VIP.
    bang_host_suffix={"bang-fiesta": 1, "bang-siesta": 2},
    inventory_hostname="bang-fiesta",
    apply_kea_v6_bootfile="efi64/ipxe.efi",
)


def _render(name, ctx):
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(f"{REPO}/roles/apply_kea/templates"),
        trim_blocks=True, lstrip_blocks=True, undefined=jinja2.StrictUndefined)
    return env.get_template(name).render(**ctx)


def _as_json(rendered):
    """Strip ${ENVSUBST} placeholders so json.loads can validate structure."""
    stripped = re.sub(r"\$\{[A-Z0-9_]+\}", "envsubst", rendered)
    return json.loads(stripped)


def test_dhcp4_renders_one_subnet_per_vlan_plus_mgmt():
    doc = _as_json(_render("kea-dhcp4.conf.j2", CTX))
    subnets = doc["Dhcp4"]["subnet4"]
    ids = [s["id"] for s in subnets]
    assert 16 in ids and 17 in ids and 31 in ids   # lab subnets keyed by vid
    assert 4 in ids                                  # mgmt subnet
    s17 = next(s for s in subnets if s["id"] == 17)
    assert s17["subnet"] == "172.17.0.0/16"
    assert s17["pools"][0]["pool"] == "172.17.0.50 - 172.17.0.200"
    assert s17["next-server"] == "172.17.0.10"


def test_dhcp4_mgmt_has_onie_and_guest_pools():
    doc = _as_json(_render("kea-dhcp4.conf.j2", CTX))
    mgmt = next(s for s in doc["Dhcp4"]["subnet4"] if s["id"] == 4)
    pools = [p["pool"] for p in mgmt["pools"]]
    assert "192.168.88.100 - 192.168.88.150" in pools   # ONIE
    assert "192.168.88.200 - 192.168.88.249" in pools   # guest
    onie_pool = next(p for p in mgmt["pools"] if p["pool"].endswith(".150"))
    assert onie_pool["client-class"] == "ONIE"


def test_dhcp4_pxe_and_onie_client_classes():
    doc = _as_json(_render("kea-dhcp4.conf.j2", CTX))
    names = {c["name"] for c in doc["Dhcp4"]["client-classes"]}
    assert {"iPXE", "BIOS", "EFI64", "ONIE"} <= names
    bios = next(c for c in doc["Dhcp4"]["client-classes"] if c["name"] == "BIOS")
    assert bios["boot-file-name"] == "bios/undionly.kpxe"
    onie = next(c for c in doc["Dhcp4"]["client-classes"] if c["name"] == "ONIE")
    assert "OnieDhcpRequest" in onie["test"]


def test_dhcp4_reads_reservations_from_hosts_database():
    doc = _as_json(_render("kea-dhcp4.conf.j2", CTX))
    assert doc["Dhcp4"]["hosts-database"]["type"] == "postgresql"


_V6 = dict(apply_kea_v6_pool_lo="100", apply_kea_v6_pool_hi="1ff",
           apply_kea_v6_lab_pool_lo="f000", apply_kea_v6_lab_pool_hi="ffff",
           apply_kea_mgmt_v6_prefix="fd00:88")


def test_dhcp6_lab_and_mgmt_subnets_when_lab_enabled():
    ctx = dict(CTX, apply_kea_v6_lab_vlans=True, **_V6)
    doc = _as_json(_render("kea-dhcp6.conf.j2", ctx))
    subnets = {s["subnet"] for s in doc["Dhcp6"]["subnet6"]}
    assert "fd00:16::/64" in subnets
    assert "fd00:31::/64" in subnets
    assert "fd00:88::/64" in subnets            # mgmt always present
    s17 = next(s for s in doc["Dhcp6"]["subnet6"] if s["subnet"] == "fd00:17::/64")
    # lab pool relocated to ::f000-::ffff (disjoint from reservation space)
    assert s17["pools"][0]["pool"] == "fd00:17::f000 - fd00:17::ffff"


def test_dhcp6_lab_subnets_hand_out_bootfile_url_at_master_static():
    """Each lab subnet6 carries a bootfile-url (option 59) pointing at the
    MASTER's own per-vlan static fd00:<vid>::<host_suffix> (no VIP). The mgmt
    subnet does NOT (no netboot on mgmt)."""
    ctx = dict(CTX, apply_kea_v6_lab_vlans=True, inventory_hostname="bang-fiesta", **_V6)
    doc = _as_json(_render("kea-dhcp6.conf.j2", ctx))
    s17 = next(s for s in doc["Dhcp6"]["subnet6"] if s["subnet"] == "fd00:17::/64")
    opt = next(o for o in s17["option-data"] if o["name"] == "bootfile-url")
    assert opt["data"] == "tftp://[fd00:17::1]/efi64/ipxe.efi"   # fiesta suffix=1
    mgmt = next(s for s in doc["Dhcp6"]["subnet6"] if s["subnet"] == "fd00:88::/64")
    assert not any(o.get("name") == "bootfile-url" for o in mgmt.get("option-data", []))


def test_dhcp6_bootfile_url_tracks_the_master_host_suffix():
    """On the standby (suffix 2) the same template renders ::2 — so whichever
    bang is master serves its own TFTP address. No config churn on failover."""
    ctx = dict(CTX, apply_kea_v6_lab_vlans=True, inventory_hostname="bang-siesta", **_V6)
    doc = _as_json(_render("kea-dhcp6.conf.j2", ctx))
    s16 = next(s for s in doc["Dhcp6"]["subnet6"] if s["subnet"] == "fd00:16::/64")
    opt = next(o for o in s16["option-data"] if o["name"] == "bootfile-url")
    assert opt["data"] == "tftp://[fd00:16::2]/efi64/ipxe.efi"   # siesta suffix=2


def test_dhcp6_mgmt_only_when_lab_disabled():
    ctx = dict(CTX, apply_kea_v6_lab_vlans=False, **_V6)
    doc = _as_json(_render("kea-dhcp6.conf.j2", ctx))
    subnets = {s["subnet"] for s in doc["Dhcp6"]["subnet6"]}
    assert subnets == {"fd00:88::/64"}          # lab vlans absent


# --- reservation-only (no pool + authoritative) on post lab-data vlans --------
# Site flag on, post-phase gated; vid 31 opts back into a pool via dhcp_mode.
RES_VLANS = [
    {"vid": 17, "family": "tiogapass", "phase": "triage", "parent": "eth0", "cidr": "172.17.0.0/16", "gateway_vip": "172.17.0.10"},
    {"vid": 25, "family": "tiogapass", "phase": "post", "parent": "eth0", "cidr": "172.25.0.0/16", "gateway_vip": "172.25.0.10"},
    {"vid": 31, "family": "grandcanyon", "phase": "post", "parent": "eth0", "cidr": "172.31.0.0/16", "gateway_vip": "172.31.0.10", "dhcp_mode": "pool"},
]
RES_CTX = dict(CTX, vlans=RES_VLANS, reservation_only_dhcp=True,
               apply_kea_reservation_only_phases=["post"])


def test_dhcp4_post_vlan_reservation_only_no_pool_authoritative():
    doc = _as_json(_render("kea-dhcp4.conf.j2", RES_CTX))
    s25 = next(s for s in doc["Dhcp4"]["subnet4"] if s["id"] == 25)
    assert s25["pools"] == []
    assert s25["authoritative"] is True
    assert s25["next-server"] == "172.25.0.10"   # reservations still PXE


def test_dhcp4_triage_keeps_pool_when_post_only():
    doc = _as_json(_render("kea-dhcp4.conf.j2", RES_CTX))
    s17 = next(s for s in doc["Dhcp4"]["subnet4"] if s["id"] == 17)
    assert s17["pools"][0]["pool"] == "172.17.0.50 - 172.17.0.200"
    assert "authoritative" not in s17


def test_dhcp4_dhcp_mode_pool_overrides_reservation_only():
    doc = _as_json(_render("kea-dhcp4.conf.j2", RES_CTX))
    s31 = next(s for s in doc["Dhcp4"]["subnet4"] if s["id"] == 31)
    assert s31["pools"][0]["pool"] == "172.31.0.50 - 172.31.0.200"
    assert "authoritative" not in s31


def test_dhcp4_mgmt_pool_unaffected_by_reservation_only():
    doc = _as_json(_render("kea-dhcp4.conf.j2", RES_CTX))
    mgmt = next(s for s in doc["Dhcp4"]["subnet4"] if s["id"] == 4)
    pools = [p["pool"] for p in mgmt["pools"]]
    assert "192.168.88.100 - 192.168.88.150" in pools
    assert "192.168.88.200 - 192.168.88.249" in pools
    assert "authoritative" not in mgmt


# ── Part 3 / E: BMC pool guard (spec 2026-09-27 §7) ───────────────────────
# NOTE: the brief's `_render_dhcp4`/`_vlan`/`_REAPER` names don't exist in
# this file; reusing the real ones (`_render`+`_as_json`, plain vlan dicts
# built off CTX, and REPO) instead, per the implementer-rules instruction to
# adapt to the file's own helpers.
_BMC_CLASSES = {
    "BMC_UDHCP": "substring(option[60].text,0,6) == 'udhcp '",
    "BMC_NETWORKD": ("option[55].hex == 0x0103060c0f212a72777879 and not option[60].exists"
                     " and not option[93].exists and not option[77].exists"
                     " and not option[175].exists"),
    # Facebook OpenBMC (ISC dhclient); permanent, reservation-only (reaper 1e499f1)
    "BMC_FB_DHCLIENT": ("option[55].hex == 0x011c02030f060c2c2f2a and not option[60].exists"
                        " and not option[93].exists and not option[77].exists"
                        " and not option[175].exists"),
    "POOL_HOSTS": ("not member('BMC_UDHCP') and not member('BMC_NETWORKD')"
                   " and not member('BMC_FB_DHCLIENT')"),
}


def test_bmc_fingerprint_classes_render_verbatim():
    doc = _as_json(_render("kea-dhcp4.conf.j2", CTX))
    classes = {c["name"]: c.get("test") for c in doc["Dhcp4"]["client-classes"]}
    for name, test in _BMC_CLASSES.items():
        assert classes[name] == test


def test_pool_guard_puts_pool_hosts_on_the_pool_only_when_flagged():
    vlans = [
        {"vid": 16, "family": "leopard", "phase": "triage", "parent": "eth0",
         "cidr": "172.16.0.0/16", "gateway_vip": "172.16.0.10", "pool_guard": True},
        {"vid": 17, "family": "tiogapass", "phase": "triage", "parent": "eth0",
         "cidr": "172.17.0.0/16", "gateway_vip": "172.17.0.10"},
        {"vid": 26, "family": "tiogapass", "phase": "triage", "parent": "eth0",
         "cidr": "172.26.0.0/16", "gateway_vip": "172.26.0.10", "pool_guard": False},
    ]
    ctx = dict(CTX, vlans=vlans)
    doc = _as_json(_render("kea-dhcp4.conf.j2", ctx))
    by_id = {s["id"]: s for s in doc["Dhcp4"]["subnet4"]}
    assert by_id[16]["pools"][0]["client-class"] == "POOL_HOSTS"
    assert "client-class" not in by_id[17]["pools"][0]
    assert "client-class" not in by_id[26]["pools"][0]


def test_eindhoven_pool_guard_only_on_vid_16_17():
    import yaml
    v = yaml.safe_load(open(os.path.join(REPO, "inventories/eindhoven/group_vars/all/vars.yml")))
    guarded = {x["vid"]: x.get("pool_guard") for x in v["vlans"] if "pool_guard" in x}
    assert guarded == {16: True, 17: True}


# --- BMC_UDHCP-only pool on reservation-only post VLANs (2026-11-06) ---------
def _s(doc, vid):
    return next(s for s in doc["Dhcp4"]["subnet4"] if s["id"] == vid)


def test_res_only_bmc_pool_admits_udhcp_only():
    doc = _as_json(_render("kea-dhcp4.conf.j2", dict(RES_CTX, apply_kea_res_only_bmc_pool=True)))
    s25 = _s(doc, 25)
    assert s25["pools"] == [{"pool": "172.25.0.50 - 172.25.0.200", "client-class": "POOL_BMC_UDHCP"}]
    assert s25["authoritative"] is True
    classes = {c["name"]: c.get("test") for c in doc["Dhcp4"]["client-classes"]}
    assert classes["POOL_BMC_UDHCP"] == "member('BMC_UDHCP')"


def test_res_only_bmc_pool_off_by_default():
    assert _s(_as_json(_render("kea-dhcp4.conf.j2", RES_CTX)), 25)["pools"] == []


def test_res_only_bmc_pool_leaves_triage_and_dhcp_mode_pool_alone():
    on = _as_json(_render("kea-dhcp4.conf.j2", dict(RES_CTX, apply_kea_res_only_bmc_pool=True)))
    off = _as_json(_render("kea-dhcp4.conf.j2", RES_CTX))
    assert _s(on, 17) == _s(off, 17)
    assert _s(on, 31) == _s(off, 31)
    assert "POOL_BMC_UDHCP" not in json.dumps(_s(on, 17)) + json.dumps(_s(on, 31))


def test_eindhoven_enables_res_only_bmc_pool():
    import yaml
    v = yaml.safe_load(open(os.path.join(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir),
                                         "inventories/eindhoven/group_vars/all/vars.yml")))
    assert v["apply_kea_res_only_bmc_pool"] is True


def _post_role(ctx):
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "roles/apply_lease_agent/templates")),
        trim_blocks=True, lstrip_blocks=True)
    env.filters["to_json"] = json.dumps
    return json.loads(env.get_template("roles.d/post.json.j2").render(**ctx))


def test_post_registry_reconcile_pool_bmc_defaults_false_and_follows_var():
    assert _post_role({})["capabilities"]["reconcile_pool_bmc"] is False
    on = _post_role({"flax_role_post_reconcile_pool_bmc": True})["capabilities"]
    assert on["reconcile_pool_bmc"] is True and on["reconcile_switch"] is False
