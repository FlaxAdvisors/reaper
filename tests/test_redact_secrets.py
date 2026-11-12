# tests/test_redact_secrets.py
"""Unit tests for filter_plugins/redact_secrets.py (RULE ZERO remediation).

The filter is what keeps cleartext device secrets out of the committed
files/configs/ tree: collect_* pipes every write into config/ through it, and
push rehydrates the {{ vault_cfg_* }} placeholders it leaves behind. A
regression here silently re-publishes secrets on the next `reaper fetch`, so
these tests pin both halves: what MUST be redacted, and what must NOT be.
"""
import importlib.util
import os

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PLUGIN = os.path.join(os.path.dirname(_HERE), "filter_plugins", "redact_secrets.py")

if _PLUGIN:
    _spec = importlib.util.spec_from_file_location("redact_secrets", _PLUGIN)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    redact_secrets = _mod.redact_secrets


# --- what must be redacted ------------------------------------------------

def test_wpa_psk_redacted_and_quoting_preserved():
    out = redact_secrets("WIRELESS_WPA_PSK='hunter2'\n")
    assert out == "WIRELESS_WPA_PSK='{{ vault_cfg_wlan_wpa_psk }}'\n"


def test_wpa_psk_unquoted_stays_unquoted():
    assert redact_secrets("WIRELESS_WPA_PSK=hunter2") == \
        "WIRELESS_WPA_PSK={{ vault_cfg_wlan_wpa_psk }}"


def test_arista_snmp_community_redacted_suffix_kept():
    assert redact_secrets("snmp-server community s3cr3t ro") == \
        "snmp-server community {{ vault_cfg_snmp_ro_community }} ro"


def test_cumulus_snmp_community_redacted():
    out = redact_secrets("net add snmp-server readonly-community s3cr3t access any")
    assert "s3cr3t" not in out
    assert "{{ vault_cfg_snmp_ro_community }}" in out
    assert out.endswith(" access any")


def test_cisco_enable_password_redacted():
    assert redact_secrets("enable password s3cr3t") == \
        "enable password {{ vault_cfg_cisco_enable_password }}"


def test_cisco_vty_password_redacted_inside_line_block():
    src = "line vty 0 4\n exec-timeout 30 0\n password s3cr3t\n transport input ssh\n"
    out = redact_secrets(src)
    assert "s3cr3t" not in out
    assert " password {{ vault_cfg_cisco_vty_password }}\n" in out


def test_console_and_aux_blocks_also_covered():
    for block in ("line con 0", "line aux 0"):
        out = redact_secrets(block + "\n password s3cr3t\n")
        assert "s3cr3t" not in out, block


# --- what must NOT be redacted (the dangerous half) -----------------------

def test_hashes_are_left_alone():
    """Operator decision 2026-08-26: hashes stay literal. Pushing a wrong hash
    into a password line locks us out of a remote lab switch."""
    for line in ("enable secret 8 $8$Xk2abc",
                 "username admin privilege 15 secret 8 $8$9fQdef",
                 "username admin role network-admin secret sha512 $6$abc$def"):
        assert redact_secrets(line) == line


def test_typed_cisco_password_left_alone():
    """`enable password 7 <encrypted>` keeps its encoding -- redacting it would
    make push write a cleartext where the device expects type-7."""
    assert redact_secrets("enable password 7 0822455D0A16") == \
        "enable password 7 0822455D0A16"


def test_password_outside_a_line_block_is_not_touched():
    """A bare indented `password` under some other sub-mode is not the vty
    password and must not be mapped onto that vault var."""
    src = "router bgp 65000\n neighbor 10.0.0.1 password s3cr3t\n"
    assert redact_secrets(src) == src


def test_line_block_ends_at_next_top_level_stanza():
    src = ("line vty 0 4\n transport input ssh\n"
           "interface Et1\n password notthevty\n")
    out = redact_secrets(src)
    assert "notthevty" in out, "sub-mode tracking leaked past the line block"


def test_unrelated_config_untouched():
    src = ("interface Ethernet6/1\n   switchport access vlan 17\n"
           "   description leopard node\n")
    assert redact_secrets(src) == src


# --- properties -----------------------------------------------------------

def test_idempotent():
    src = ("WIRELESS_WPA_PSK='hunter2'\nsnmp-server community s3cr3t ro\n"
           "line vty 0 4\n password s3cr3t\n")
    once = redact_secrets(src)
    assert redact_secrets(once) == once


def test_line_count_and_trailing_newline_preserved():
    src = "enable password s3cr3t\nhostname turtle\n"
    out = redact_secrets(src)
    assert out.endswith("\n")
    assert len(out.splitlines()) == len(src.splitlines())


def test_no_trailing_newline_stays_absent():
    assert not redact_secrets("hostname turtle").endswith("\n")


@pytest.mark.parametrize("value", [None, 42, "", b"bytes"])
def test_non_string_passthrough(value):
    assert redact_secrets(value) == value


def test_empty_secret_value_not_replaced():
    """lavergne's `net add dot1x radius shared-secret ` has no value; an empty
    capture must not become a placeholder."""
    assert redact_secrets("WIRELESS_WPA_PSK=") == "WIRELESS_WPA_PSK="


# --- SONiC config_db JSON (roles/sonic -> config/config_db.json) -----------

import json  # noqa: E402

_SONIC_DB = {
    "DEVICE_METADATA": {"localhost": {"hostname": "sonic", "hwsku": "DellEMC-N3248TE"}},
    "SNMP": {"LOCATION": {"Location": "public"}},
    "SNMP_COMMUNITY": {"fakeRO1": {"TYPE": "RO"}, "fakeRW1": {"TYPE": "RW"}},
    "SNMP_USER": {"monitor": {"SNMP_USER_TYPE": "Priv",
                              "SNMP_USER_AUTH_TYPE": "SHA",
                              "SNMP_USER_AUTH_PASSWORD": "fakeAuthPw",
                              "SNMP_USER_ENCRYPTION_TYPE": "AES",
                              "SNMP_USER_ENCRYPTION_PASSWORD": "fakePrivPw"}},
    "TACPLUS": {"global": {"auth_type": "pap", "passkey": "fakeTacGlobal"}},
    "TACPLUS_SERVER": {"10.0.0.9": {"priority": "1", "passkey": "fakeTacSrv"}},
    "RADIUS_SERVER": {"10.0.0.8": {"passkey": "fakeRadSrv"}},
    "NTP_KEY": {"1": {"type": "md5", "value": "fakeNtpKey"}},
    "WIDGET": {"x": {"admin_password": "fakeFallback"}},
    "PASSW_HARDENING": {"POLICIES": {"reject_user_passw_match": "true",
                                     "len_min": "8"}},
    "bgpraw": "router bgp 65000\nsnmp-server community fakeFrr ro\n",
}
_FAKES = ("fakeRO1", "fakeRW1", "fakeAuthPw", "fakePrivPw", "fakeTacGlobal",
          "fakeTacSrv", "fakeRadSrv", "fakeNtpKey", "fakeFallback", "fakeFrr")


def _sonic(db=None):
    # SONiC prints a leading blank line before the JSON.
    return "\n" + json.dumps(_SONIC_DB if db is None else db, indent=4) + "\n"


def test_sonic_no_fake_secret_survives():
    out = redact_secrets(_sonic())
    for fake in _FAKES:
        assert fake not in out, fake


def test_sonic_community_key_becomes_placeholder_by_type():
    db = json.loads(redact_secrets(_sonic()))
    assert db["SNMP_COMMUNITY"] == {
        "{{ vault_cfg_snmp_ro_community }}": {"TYPE": "RO"},
        "{{ vault_cfg_snmp_rw_community }}": {"TYPE": "RW"}}


def test_sonic_second_community_of_same_type_is_not_collapsed():
    db = json.loads(redact_secrets(_sonic(
        {"SNMP_COMMUNITY": {"a1": {"TYPE": "RO"}, "b2": {"TYPE": "RO"}}})))
    assert sorted(db["SNMP_COMMUNITY"]) == [
        "{{ vault_cfg_snmp_ro_community }}", "{{ vault_cfg_snmp_ro_community_2 }}"]


def test_sonic_named_secret_fields_map_to_their_vault_vars():
    db = json.loads(redact_secrets(_sonic()))
    u = db["SNMP_USER"]["monitor"]
    assert u["SNMP_USER_AUTH_PASSWORD"] == "{{ vault_cfg_snmp_user_auth_password }}"
    assert u["SNMP_USER_ENCRYPTION_PASSWORD"] == "{{ vault_cfg_snmp_user_priv_password }}"
    assert u["SNMP_USER_AUTH_TYPE"] == "SHA"
    assert db["TACPLUS"]["global"] == {"auth_type": "pap",
                                       "passkey": "{{ vault_cfg_tacplus_passkey }}"}
    assert db["TACPLUS_SERVER"]["10.0.0.9"]["passkey"] == "{{ vault_cfg_tacplus_passkey }}"
    assert db["RADIUS_SERVER"]["10.0.0.8"]["passkey"] == "{{ vault_cfg_radius_passkey }}"
    assert db["NTP_KEY"]["1"] == {"type": "md5", "value": "{{ vault_cfg_ntp_key }}"}


def test_sonic_unknown_table_secret_field_gets_backstop_var():
    db = json.loads(redact_secrets(_sonic()))
    assert db["WIDGET"]["x"]["admin_password"] == \
        "{{ vault_cfg_sonic_widget_admin_password }}"


def test_sonic_non_secrets_untouched():
    """SNMP|LOCATION is stock "public" (a location, not a community), and
    PASSW_HARDENING is policy, not a password."""
    db = json.loads(redact_secrets(_sonic()))
    assert db["SNMP"] == _SONIC_DB["SNMP"]
    assert db["PASSW_HARDENING"] == _SONIC_DB["PASSW_HARDENING"]
    assert db["DEVICE_METADATA"] == _SONIC_DB["DEVICE_METADATA"]


def test_sonic_bgpraw_goes_through_line_rules():
    db = json.loads(redact_secrets(_sonic()))
    assert db["bgpraw"] == ("router bgp 65000\n"
                            "snmp-server community {{ vault_cfg_snmp_ro_community }} ro\n")


def test_sonic_output_is_sonic_format_and_idempotent():
    out = redact_secrets(_sonic())
    assert out.startswith("{\n    \"DEVICE_METADATA\"")   # indent 4, order kept
    assert out.endswith("}\n")
    assert redact_secrets(out) == out


def test_sonic_without_secrets_round_trips_unchanged():
    db = {"PORT": {"Ethernet0": {"admin_status": "up", "speed": "1000"}}}
    assert redact_secrets(_sonic(db)) == json.dumps(db, indent=4) + "\n"


def test_sonic_unparsable_json_fails_closed():
    """A truncated config_db must not fall through to the line rules, which
    know nothing about a community stored as a JSON key."""
    with pytest.raises(ValueError):
        redact_secrets('{\n    "SNMP_COMMUNITY": {\n        "fakeRO1": {')


# --- roles/sonic's explicit filter: redact_sonic_config_db ------------------

def test_sonic_filter_strips_leading_noise_and_still_redacts():
    """raw merges stderr into stdout; a line before the `{` must not send the
    config_db down the line rules (which miss a community stored as a key)."""
    out = _mod.redact_sonic_config_db("No such container: bgp\r\n" + _sonic())
    for fake in _FAKES:
        assert fake not in out, fake
    assert out.startswith("{\n")
    assert json.loads(out)["SNMP_COMMUNITY"] == {
        "{{ vault_cfg_snmp_ro_community }}": {"TYPE": "RO"},
        "{{ vault_cfg_snmp_rw_community }}": {"TYPE": "RW"}}


@pytest.mark.parametrize("text", [
    "",
    "sudo: a password is required\n",
    "error {not json}\n",
    '["not", "an", "object"]\n',
    '{\n    "SNMP_COMMUNITY": {\n        "fakeRO1": {',
    json.dumps({"SNMP_COMMUNITY": {"fakeRO1": {"TYPE": "RO"}}}) + "\ntrailing junk\n",
])
def test_sonic_filter_without_a_clean_object_fails(text):
    with pytest.raises(ValueError):
        _mod.redact_sonic_config_db(text)


def test_sonic_filter_is_idempotent():
    once = _mod.redact_sonic_config_db(_sonic())
    assert _mod.redact_sonic_config_db(once) == once


def test_sonic_secret_containing_braces_is_still_redacted():
    """Only the exact `{{ vault_cfg_x }}` form counts as already redacted."""
    db = {"SNMP_COMMUNITY": {"fake{{comm": {"TYPE": "RO"}},
          "TACPLUS": {"global": {"passkey": "{{fakeTac}}"}},
          "WIDGET": {"x": {"admin_password": "{{ vault_cfg_x }}tail"}},
          "bgpraw": "snmp-server community {{fakeFrr ro\n"}
    out = _mod.redact_sonic_config_db(json.dumps(db))
    for fake in ("fake{{comm", "{{fakeTac}}", "}}tail", "{{fakeFrr"):
        assert fake not in out, fake
    red = json.loads(out)
    assert list(red["SNMP_COMMUNITY"]) == ["{{ vault_cfg_snmp_ro_community }}"]
    assert red["TACPLUS"]["global"]["passkey"] == "{{ vault_cfg_tacplus_passkey }}"
    assert red["bgpraw"] == "snmp-server community {{ vault_cfg_snmp_ro_community }} ro\n"


def test_line_secret_containing_braces_is_still_redacted():
    out = redact_secrets("snmp-server community {{fake ro\n")
    assert out == "snmp-server community {{ vault_cfg_snmp_ro_community }} ro\n"
