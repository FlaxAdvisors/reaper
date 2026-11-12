"""FOLLOWUP-2026-11-08-health-noise §5/§6: units that only ever show as failed, and the
node_exporter drop-in systemd warns about on every start."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DROPIN = ROOT / "roles/apply_node_exporter/templates/prometheus-node_exporter.service.d/override.conf.j2"
TASKS = ROOT / "roles/apply_bang_services/tasks/main.yml"
KNOWN = set("abfnrtv\\\"'s") | {"x", "u", "U"} | set("01234567")   # systemd.syntax C escapes


def unescape(word):
    """What systemd passes for one ExecStart word, plus the escapes it does not know."""
    out, unknown, i = [], [], 0
    while i < len(word):
        c = word[i]
        if c == "\\" and i + 1 < len(word):
            n = word[i + 1]
            if n not in KNOWN:
                unknown.append("\\" + n)
            out.append("\\" if n == "\\" else "\\" + n)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out), unknown


def test_node_exporter_unit_regex_has_no_unknown_escape_and_still_matches_a_dot():
    # The drop-in renders the generated regex (healthboard.unit_include_regex) with its
    # backslashes doubled; systemd then unescapes it once.
    from reapercli.healthboard import unit_include_regex
    from reapercli.lib.components import load_components
    from reapercli.lib.sites import load_sites
    assert "replace('\\\\', '\\\\\\\\')" in DROPIN.read_text()
    rendered = unit_include_regex(load_components(ROOT, load_sites(ROOT))).replace("\\", "\\\\")
    regex, unknown = unescape(rendered)
    assert unknown == []
    assert re.fullmatch(regex, "dnsmasq.service") and re.fullmatch(regex, "kea-dhcp4.service")
    assert not re.fullmatch(regex, "dnsmasqXservice")


def _tasks():
    out = []
    for t in yaml.safe_load(TASKS.read_text()):
        out += t["block"] if "block" in t else [t]
    return out


def test_isc_dhcpd_is_masked_where_kea_serves_dhcp():
    [t] = [t for t in _tasks() if (t.get("ansible.builtin.systemd") or {}).get("name") == "dhcpd"]
    assert t["ansible.builtin.systemd"]["masked"] is True and "'kea'" in str(t["when"])


def test_kdump_units_are_disabled_without_a_crashkernel():
    names = [n for t in _tasks() if (t.get("ansible.builtin.systemd") or {}).get("enabled") is False
             for n in t.get("loop") or [t["ansible.builtin.systemd"]["name"]]]
    assert {"kdump", "kdump-early"} <= set(names)


def test_failed_leftovers_are_reset():
    assert any("reset-failed" in str(t.get("ansible.builtin.shell", "")) for t in _tasks())
