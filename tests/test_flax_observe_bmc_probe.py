"""Tests for flax_observe.bmc_probe.

Covers:
  - _parse_fru_product_name: both ipmitool-fru and weutil formats
  - probe_bmc_kind: FRU chain emitted via ssh_runner returns product_name
                   from a weutil blob (no network)
"""

import pytest

from flax_observe.bmc_probe import (
    _parse_watts_from_ipmi_output,
    probe_bmc_kind,
)
from flax_observe.fru import read_baseboard


# ---------------------------------------------------------------------------
# Fixtures — realistic FRU blobs
# ---------------------------------------------------------------------------

# Live weutil output from ssh root@192.168.88.14 weutil on rabbit-hare
WEUTIL_BLOB = """\
Wedge EEPROM :
Product Name: WEDGE100S12V
Product Part Number: 20-001688
Product Serial Number: AI09019591
Product Production State: 4
Product Asset Tag: N/A
Product Custom Data 1: N/A
Product Custom Data 2: N/A
Product Custom Data 3: N/A
"""

# Realistic ipmitool-fru output (Leopard / Tioga-Pass style)
IPMITOOL_FRU_BLOB = """\
FRU Device Description : Builtin FRU Device (ID 0)
 Board Mfg Date        : Mon Jan  1 00:00:00 1996
 Board Mfg             : Wiwynn
 Board Product         : TP-20
 Board Serial          : WI20B00000
 Product Manufacturer  : Wiwynn
 Product Name         : Leopard
 Product Part Number  : TP-20
 Product Serial       : WI20B00000
 Product Asset Tag    :
"""


# ---------------------------------------------------------------------------
# product name via read_baseboard
# ---------------------------------------------------------------------------

class TestParseFruProductName:
    def test_weutil_returns_product_name(self):
        """weutil format: 'Product Name: WEDGE100S12V' — no extra spaces."""
        result = read_baseboard(WEUTIL_BLOB, {})["product_name"]
        assert result == "WEDGE100S12V"

    def test_weutil_does_not_return_part_number(self):
        """Must not match 'Product Part Number' instead of 'Product Name'."""
        result = read_baseboard(WEUTIL_BLOB, {})["product_name"]
        assert result != "20-001688"
        assert "Part" not in (result or "")

    def test_weutil_does_not_return_serial_number(self):
        """Must not match 'Product Serial Number'."""
        result = read_baseboard(WEUTIL_BLOB, {})["product_name"]
        assert result != "AI09019591"

    def test_ipmitool_fru_returns_product_name(self):
        """ipmitool-fru format: 'Product Name         : Leopard'."""
        result = read_baseboard(IPMITOOL_FRU_BLOB, {})["product_name"]
        assert result == "Leopard"

    def test_ipmitool_fru_does_not_return_part_number(self):
        """Must not match 'Product Part Number'."""
        result = read_baseboard(IPMITOOL_FRU_BLOB, {})["product_name"]
        assert result != "TP-20"

    def test_empty_text_returns_none(self):
        assert read_baseboard("", {})["product_name"] is None

    def test_no_product_name_returns_none(self):
        blob = "Board Serial: XYZ\nProduct Serial: 12345\n"
        assert read_baseboard(blob, {})["product_name"] is None

    def test_asset_tag_and_production_state_not_matched(self):
        """Production State / Asset Tag lines are not returned."""
        result = read_baseboard(WEUTIL_BLOB, {})["product_name"]
        assert result not in ("4", "N/A")


# ---------------------------------------------------------------------------
# Integration tests for probe_bmc_kind (no network — injected ssh_runner)
# ---------------------------------------------------------------------------

def _fake_os_release_openbmc():
    return "ID=openbmc\nVERSION_ID=v2.9.0"


def test_probe_bmc_kind_weutil_blob_returns_product_name():
    """When ssh returns a weutil blob as FRU, probe_bmc_kind picks up the
    product_name correctly as 'WEDGE100S12V'."""
    calls = []

    def fake_ssh_runner(host, user, password, cmd, timeout=8):
        calls.append(cmd)
        if "os-release" in cmd:
            return _fake_os_release_openbmc()
        # FRU command (any of the chain variants)
        return WEUTIL_BLOB

    creds = {"obmcuser": "root", "obmcpass": "test-obmc-pw"}
    result = probe_bmc_kind(
        ip="192.168.88.14",
        credentials=creds,
        bmc_creds=[],
        ssh_runner=fake_ssh_runner,
        port_probe=lambda h: {"ssh": True, "ipmi": False},
    )

    assert result["kind"] == "openbmc"
    assert result["product_name"] == "WEDGE100S12V"
    assert result["creds_used"] == ("root", "test-obmc-pw")


def test_probe_bmc_kind_openbmc_reads_product_name_over_lan_when_ssh_fru_is_absent():
    # Measured 2026-09-14 on eindhoven et10b2/et10b3/et8b1/et8b2: `ipmitool fru
    # print 0` run on the BMC says "Device not present" while the same read over
    # LAN IPMI returns the baseboard FRU.
    lan_calls = []

    def fake_ssh_runner(host, user, password, cmd, timeout=8):
        if "os-release" in cmd:
            return _fake_os_release_openbmc()
        return "Device not present (Destination unavailable)\n"

    def fake_ipmi_runner(host, user, password, args):
        lan_calls.append(args)
        return IPMITOOL_FRU_BLOB

    result = probe_bmc_kind(
        ip="172.17.10.102",
        credentials={"obmcuser": "root", "obmcpass": "test-obmc-pw"},
        bmc_creds=[{"bmcuser": "USERID", "bmcpass": "x"}],
        ssh_runner=fake_ssh_runner,
        ipmi_runner=fake_ipmi_runner,
        port_probe=lambda h: {"ssh": True, "ipmi": True},
    )

    assert result["kind"] == "openbmc"
    assert result["product_name"] == "Leopard"
    assert result["creds_used"] == ("root", "test-obmc-pw")
    assert lan_calls == [["fru", "print", "0"]]


def test_probe_bmc_kind_default_port_probe_lets_ssh_port_settle_before_ssh(monkeypatch):
    # Measured 2026-09-14 on eindhoven et26b3 (dropbear 2025.88): a bare TCP
    # connect to :22 immediately followed by ssh failed rc=255 3/3, with a 1-2 s
    # gap ssh succeeded 4/4. The probe then fell through to the Redfish branch
    # and re-labelled a working OpenBMC "Ami Redfish".
    from flax_observe import bmc_probe
    clock = {"now": 100.0}
    events = []

    def fake_sleep(secs):
        clock["now"] += secs

    def fake_tcp(host, port, timeout=1.0):
        if port == 22:
            events.append(("tcp22", clock["now"]))
        return True

    def fake_ssh(host, user, password, cmd, timeout=8):
        events.append(("ssh", clock["now"]))
        return _fake_os_release_openbmc() if "os-release" in cmd else IPMITOOL_FRU_BLOB

    monkeypatch.setattr(bmc_probe, "_monotonic", lambda: clock["now"], raising=False)
    monkeypatch.setattr(bmc_probe, "_sleep", fake_sleep, raising=False)
    monkeypatch.setattr(bmc_probe, "_tcp_port_open", fake_tcp)
    monkeypatch.setattr(bmc_probe, "_ipmi_responsive", lambda h, timeout=2.0: False)

    result = bmc_probe.probe_bmc_kind(
        "172.17.26.103", {"obmcuser": "root", "obmcpass": "pw"}, [],
        ssh_runner=fake_ssh)

    assert result["kind"] == "openbmc"
    t22 = next(t for e, t in events if e == "tcp22")
    t_ssh = next(t for e, t in events if e == "ssh")
    assert t_ssh - t22 >= 1.0


def test_probe_bmc_kind_weutil_fru_command_includes_weutil():
    """The FRU command issued over ssh must include 'weutil' as a fallback."""
    fru_commands = []

    def fake_ssh_runner(host, user, password, cmd, timeout=8):
        if "os-release" in cmd:
            return _fake_os_release_openbmc()
        fru_commands.append(cmd)
        return ""

    creds = {"obmcuser": "root", "obmcpass": "test-obmc-pw"}
    probe_bmc_kind(
        ip="192.168.88.14",
        credentials=creds,
        bmc_creds=[],
        ssh_runner=fake_ssh_runner,
        port_probe=lambda h: {"ssh": True, "ipmi": False},
    )

    assert fru_commands, "No FRU command was issued"
    assert "weutil" in fru_commands[0]


def test_probe_bmc_kind_ipmitool_fru_blob_returns_product_name():
    """When ssh returns an ipmitool-fru blob, product_name == 'Leopard'."""
    def fake_ssh_runner(host, user, password, cmd, timeout=8):
        if "os-release" in cmd:
            return _fake_os_release_openbmc()
        return IPMITOOL_FRU_BLOB

    creds = {"obmcuser": "root", "obmcpass": "test-obmc-pw"}
    result = probe_bmc_kind(
        ip="10.0.0.1",
        credentials=creds,
        bmc_creds=[],
        ssh_runner=fake_ssh_runner,
        port_probe=lambda h: {"ssh": True, "ipmi": False},
    )

    assert result["kind"] == "openbmc"
    assert result["product_name"] == "Leopard"


def test_probe_bmc_kind_fru_reads_builtin_device_only():
    # A bare `ipmitool fru` walks every FRU device and outlasts the 8 s ssh timeout.
    fru_commands = []

    def fake_ssh_runner(host, user, password, cmd, timeout=8):
        if "os-release" in cmd:
            return _fake_os_release_openbmc()
        fru_commands.append(cmd)
        return IPMITOOL_FRU_BLOB

    probe_bmc_kind(
        ip="10.0.0.1",
        credentials={"obmcuser": "root", "obmcpass": "test-obmc-pw"},
        bmc_creds=[],
        ssh_runner=fake_ssh_runner,
        port_probe=lambda h: {"ssh": True, "ipmi": False},
    )

    assert fru_commands[0].startswith("ipmitool fru print 0 ")


def test_chassis_serial_openbmc_reads_builtin_device_only(monkeypatch):
    from flax_observe import bmc_probe
    commands = []

    def fake_ssh_runner(host, user, password, cmd, timeout=8):
        commands.append(cmd)
        return IPMITOOL_FRU_BLOB

    monkeypatch.setattr(bmc_probe, "_default_ssh_runner", fake_ssh_runner)
    assert bmc_probe.chassis_serial_openbmc("10.0.0.1", ("root", "pw"), family_map={}) == ("WI20B00000", "ok")
    assert commands[0].startswith("ipmitool fru print 0 ")


def test_probe_bmc_kind_unknown_when_no_ports():
    """Port probe returns nothing reachable — result is kind='unknown'."""
    result = probe_bmc_kind(
        ip="192.0.2.1",
        credentials={"obmcuser": "root", "obmcpass": "x"},
        bmc_creds=[],
        port_probe=lambda h: {"ssh": False, "ipmi": False},
    )
    assert result["kind"] == "unknown"
    assert result["product_name"] is None


def test_probe_bmc_kind_redfish_identifies_bmc():
    """A Redfish-first BMC (tcp:443 serving /redfish/v1/) with no SSH-openbmc
    and no IPMI-623 must be identified as kind='redfish' -- the braintree
    TiogaPass AMI BMCs (SSH closed / AMI, Redfish only). Without this branch
    they bin as 'unknown' and observe never comms-confirms them."""
    result = probe_bmc_kind(
        ip="172.17.8.101",
        credentials={"obmcuser": "root", "obmcpass": "x"},
        bmc_creds=[{"bmcuser": "USERID", "bmcpass": "x"}],
        port_probe=lambda h: {"ssh": False, "ipmi": False, "redfish": True},
        redfish_probe=lambda h, creds: {"is_bmc": True,
                                        "product_name": "AMI Tioga Pass"},
    )
    assert result["kind"] == "redfish"
    assert result["product_name"] == "AMI Tioga Pass"


def test_probe_bmc_kind_redfish_propagates_redfish_version():
    """probe_bmc_kind must surface the redfish probe's redfish_version so
    observe_state carries it (bmc-fw uses the low OEM RedfishVersion to know a
    board is un-updatable)."""
    result = probe_bmc_kind(
        ip="172.17.8.101",
        credentials={"obmcuser": "root", "obmcpass": "x"},
        bmc_creds=[],
        port_probe=lambda h: {"ssh": False, "ipmi": False, "redfish": True},
        redfish_probe=lambda h, creds: {"is_bmc": True,
                                        "product_name": "AMI Redfish Server",
                                        "redfish_version": "1.5.0"},
    )
    assert result["kind"] == "redfish"
    assert result["redfish_version"] == "1.5.0"


def test_default_redfish_probe_version_and_product_fallback(monkeypatch):
    """_default_redfish_probe reads RedfishVersion from the (unauth) service
    root -- host-power-independent -- and falls product_name back to the root
    Product ('AMI Redfish Server') when the authed Systems Model is blank (the
    host-off / blank-SMBIOS case on these AMI boards)."""
    import flax_observe.bmc_probe as bp
    responses = {
        "/redfish/v1/": {"@odata.type": "#ServiceRoot.v1_5_0.ServiceRoot",
                         "Managers": {"@odata.id": "/redfish/v1/Managers"},
                         "RedfishVersion": "1.5.0", "Product": "AMI Redfish Server"},
        "/redfish/v1/Systems": {"Members": [{"@odata.id": "/redfish/v1/Systems/Self"}]},
        "/redfish/v1/Systems/Self": {"Model": " ", "Manufacturer": " "},
    }
    monkeypatch.setattr(bp, "_redfish_get_json",
                        lambda ip, path, cred, timeout: responses.get(path))
    info = bp._default_redfish_probe("1.2.3.4",
                                     [{"bmcuser": "Administrator", "bmcpass": "x"}])
    assert info["is_bmc"] is True
    assert info["redfish_version"] == "1.5.0"
    assert info["product_name"] == "AMI Redfish Server"


def test_default_redfish_probe_synthesizes_from_oem_when_no_product(monkeypatch):
    """An AMI build (v1.17.0) that exposes NO Systems.Model AND no root Product,
    only an Oem vendor block, must still get a stable product_name so it becomes
    a bmc-fw candidate instead of reading 'unknown'."""
    import flax_observe.bmc_probe as bp
    responses = {
        "/redfish/v1/": {"@odata.type": "#ServiceRoot.v1_17_0.ServiceRoot",
                         "Managers": {"@odata.id": "/redfish/v1/Managers"},
                         "RedfishVersion": "1.17.0",
                         "Oem": {"Ami": {"x": 1}}},
        "/redfish/v1/Systems": {"Members": [{"@odata.id": "/redfish/v1/Systems/Self"}]},
        "/redfish/v1/Systems/Self": {"Model": " ", "Manufacturer": " "},
    }
    monkeypatch.setattr(bp, "_redfish_get_json",
                        lambda ip, path, cred, timeout: responses.get(path))
    info = bp._default_redfish_probe("1.2.3.4",
                                     [{"bmcuser": "Administrator", "bmcpass": "x"}])
    assert info["product_name"] == "Ami Redfish"
    assert info["redfish_version"] == "1.17.0"


def test_default_redfish_probe_prefers_systems_model_when_present(monkeypatch):
    """When the authed Systems read yields a real Manufacturer/Model (host
    powered), that wins over the generic root Product."""
    import flax_observe.bmc_probe as bp
    responses = {
        "/redfish/v1/": {"@odata.type": "#ServiceRoot", "Managers": {},
                         "RedfishVersion": "1.17.0", "Product": "AMI Redfish Server"},
        "/redfish/v1/Systems": {"Members": [{"@odata.id": "/redfish/v1/Systems/Self"}]},
        "/redfish/v1/Systems/Self": {"Model": "TiogaPass", "Manufacturer": "Wiwynn"},
    }
    monkeypatch.setattr(bp, "_redfish_get_json",
                        lambda ip, path, cred, timeout: responses.get(path))
    info = bp._default_redfish_probe("1.2.3.4",
                                     [{"bmcuser": "Administrator", "bmcpass": "x"}])
    assert info["product_name"] == "Wiwynn TiogaPass"
    assert info["redfish_version"] == "1.17.0"


def test_probe_bmc_kind_redfish_open_but_not_a_bmc():
    """tcp:443 open but /redfish/v1/ is not a Redfish service root -> unknown,
    never a false BMC (443 alone is not proof)."""
    result = probe_bmc_kind(
        ip="10.0.0.9",
        credentials={"obmcuser": "root", "obmcpass": "x"},
        bmc_creds=[],
        port_probe=lambda h: {"ssh": False, "ipmi": False, "redfish": True},
        redfish_probe=lambda h, creds: {"is_bmc": False, "product_name": None},
    )
    assert result["kind"] == "unknown"


def test_probe_bmc_kind_ssh_openbmc_wins_over_redfish():
    """Eindhoven-safety: an OpenBMC that exposes BOTH ssh(openbmc) and 443
    must still resolve via the SSH path as kind='openbmc' -- the Redfish
    branch is a fallback for what ssh/ipmi miss, never a regression of the
    existing OpenBMC identification."""
    def fake_ssh_runner(host, user, password, cmd, timeout=8):
        if "os-release" in cmd:
            return _fake_os_release_openbmc()
        return IPMITOOL_FRU_BLOB

    redfish_calls = []
    result = probe_bmc_kind(
        ip="192.168.88.14",
        credentials={"obmcuser": "root", "obmcpass": "test-obmc-pw"},
        bmc_creds=[],
        ssh_runner=fake_ssh_runner,
        port_probe=lambda h: {"ssh": True, "ipmi": False, "redfish": True},
        redfish_probe=lambda h, creds: redfish_calls.append(h) or {"is_bmc": True},
    )
    assert result["kind"] == "openbmc"
    assert redfish_calls == [], "redfish branch must not run once ssh identified openbmc"


# ---------------------------------------------------------------------------
# _parse_watts_from_ipmi_output — input power from the SDR table
# ---------------------------------------------------------------------------

# Real `ipmitool sdr` excerpt from an eindhoven Wiwynn Type6 Tioga Pass
# (WTH491251KM1A / et25b4). The OEM firmware truncates the sensor-ID field to
# 16 chars and carries an "MB " prefix, so the input-power sensor reads
# "MB HSC In Power" (Input -> In). The same dump also has HSC *current*,
# *voltage* and *temperature* rows that the parser must NOT mistake for power.
WIWYNN_SDR = (
    "MB HSC Out Curre | 1 Amps            | ok\n"
    "MB HSC In Power  | 11.80 Watts       | ok\n"
    "MB VR PCH PVNN O | 0 Watts           | ok\n"
    "MB HSC Temperatu | 30 degrees C      | ok\n"
    "MB HSC In Voltag | 12.98 Volts       | ok\n"
)

# Legacy, un-truncated sensor label from the older boards already in triage.
LEGACY_SDR = (
    "Chassis Power is on\n"
    "HSC Input Power  | 132 Watts          | ok\n"
    "DIMM A1 Temp     | 35 degrees C       | ok\n"
)


def test_parse_watts_wiwynn_truncated_label():
    """Wiwynn OEM FW truncates the sensor name to 'MB HSC In Power' — the
    parser must still extract its Watts value."""
    assert _parse_watts_from_ipmi_output(WIWYNN_SDR) == "11.80 W"


def test_parse_watts_legacy_untruncated_label():
    """The original 'HSC Input Power' label keeps working (no regression)."""
    assert _parse_watts_from_ipmi_output(LEGACY_SDR) == "132 W"


def test_parse_watts_ignores_hsc_current_voltage_temp():
    """The HSC current/voltage/temperature rows share the 'HSC' name but are
    not power — the parser must return the Watts row, never those."""
    watts = _parse_watts_from_ipmi_output(WIWYNN_SDR)
    assert watts == "11.80 W"
    assert "Amps" not in watts and "Volts" not in watts and "degrees" not in watts


def test_parse_watts_none_when_no_power_line():
    """No HSC input-power row at all -> None (power-off chassis / missing sensor)."""
    assert _parse_watts_from_ipmi_output(
        "Chassis Power is off\nDIMM A1 Temp | 35 degrees C | ok\n") is None


# ---------------------------------------------------------------------------
# bmc_power_and_sdr_traditional — watts read regardless of on/off
# ---------------------------------------------------------------------------

def test_power_and_sdr_reports_watts_when_off(monkeypatch):
    """A powered-OFF host still draws standby watts through the HSC. The probe
    must report that real reading (not mask it), so the UI can show the draw
    while the on/off status conveys that it is off."""
    from flax_observe import bmc_probe
    off_with_standby = (
        "Chassis Power is off\n"
        "MB HSC In Power  | 11.80 Watts       | ok\n"
    )
    monkeypatch.setattr(bmc_probe, "_default_ipmi_runner",
                        lambda *a, **kw: off_with_standby)
    pwr, watts = bmc_probe.bmc_power_and_sdr_traditional("1.2.3.4", ("u", "p"))
    assert pwr == "off"
    assert watts == "11.80 W"


# ---------------------------------------------------------------------------
# _default_ssh_runner — one-shot command must NOT force a pseudo-TTY
# ---------------------------------------------------------------------------

def test_default_ssh_runner_does_not_force_pty(monkeypatch):
    """The one-shot command runner must not allocate a pseudo-TTY.

    On Phosphor OpenBMC (Tioga Pass) boards, `ssh -tt host cmd` drops into an
    interactive console that returns NO command stdout and exits non-zero, so
    `cat /etc/os-release` yields nothing -> the openbmc branch of
    probe_bmc_kind can never fire and the board misclassifies as 'redfish'
    (OEM) off the flaky IPMI fallback. Assert the runner never forces a TTY and
    still delivers the command + captured stdout.
    """
    from flax_observe import bmc_probe
    captured = {}

    def fake_check_output(argv, **kwargs):
        captured["argv"] = argv
        return "ID=openbmc-phosphor\n"

    monkeypatch.setattr(bmc_probe.subprocess, "check_output", fake_check_output)
    out = bmc_probe._default_ssh_runner("10.0.0.1", "root", "pw",
                                        "cat /etc/os-release")
    assert out == "ID=openbmc-phosphor\n"
    argv = captured["argv"]
    assert "-tt" not in argv and "-t" not in argv, argv
    assert argv[-1] == "cat /etc/os-release"


# --- vendor lane (spec 2026-09-18) -----------------------------------------
from flax_observe import bmc_vendor as _bv

_PHOSPHOR_OSREL = (
    'ID=openbmc-phosphor\n'
    'NAME="Phosphor OpenBMC (Phosphor OpenBMC Project Reference Distro)"\n'
    'VERSION_ID=flax-onetree-1.1.2-202609091341\n'
)


def _ports(ssh=False, ipmi=False, redfish=False):
    return lambda h: {"ssh": ssh, "ipmi": ipmi, "redfish": redfish}


def test_probe_emits_phosphor_vendor_and_leaves_kind_frozen():
    """kind stays 'openbmc' -- it is frozen during the additive migration and
    role_confirm / triage_compat / triage_view still read it.

    The fb-utils marker is now read in the SAME ssh call as os-release (one
    call, not three): the mock's single 'os-release' branch must emit both
    the os-release text AND the FLAX_FBUTILS= marker line."""
    def ssh(ip, user, pw, cmd):
        if "os-release" in cmd:
            return _PHOSPHOR_OSREL + "FLAX_FBUTILS=no\n"
        return ""

    out = probe_bmc_kind("1.2.3.4", {"obmcuser": "u", "obmcpass": "p"}, [],
                         ssh_runner=ssh, port_probe=_ports(ssh=True))
    assert out["vendor"] == _bv.PHOSPHOR
    assert out["kind"] == "openbmc"


def test_probe_emits_facebook_when_the_util_family_is_present():
    def ssh(ip, user, pw, cmd):
        if "os-release" in cmd:
            return _PHOSPHOR_OSREL + "FLAX_FBUTILS=yes\n"  # same ID -- the worst case
        return ""

    out = probe_bmc_kind("1.2.3.4", {"obmcuser": "u", "obmcpass": "p"}, [],
                         ssh_runner=ssh, port_probe=_ports(ssh=True))
    assert out["vendor"] == _bv.FACEBOOK
    assert out["kind"] == "openbmc"


def test_probe_vendor_is_unknown_when_the_marker_is_absent():
    """The fb-utils probe used to be a separate ssh call that could raise on
    its own; now it is folded into the os-release read, so the only way the
    second signal can fail is silently -- the FLAX_FBUTILS= marker line is
    simply missing from that read's output (a truncated read, an odd shell,
    etc). Missing must not silently become 'absent' (False); it must be
    UNKNOWN, so a Facebook board is never misrouted to IPMI it cannot serve.
    kind still resolves 'openbmc' -- os-release itself was read fine."""
    def ssh(ip, user, pw, cmd):
        if "os-release" in cmd:
            return _PHOSPHOR_OSREL      # no FLAX_FBUTILS= marker line at all
        return ""

    out = probe_bmc_kind("1.2.3.4", {"obmcuser": "u", "obmcpass": "p"}, [],
                         ssh_runner=ssh, port_probe=_ports(ssh=True))
    assert out["vendor"] == _bv.UNKNOWN
    assert out["kind"] == "openbmc"


def test_traditional_kind_is_ami_legacy_vendor():
    def ipmi(ip, user, pw, args):
        return "Product Name         : Tioga Pass\n"

    out = probe_bmc_kind("1.2.3.4", {"obmcuser": "u", "obmcpass": "p"},
                         [{"bmcuser": "U", "bmcpass": "P"}],
                         ipmi_runner=ipmi, port_probe=_ports(ipmi=True))
    assert out["kind"] == "traditional"
    assert out["vendor"] == _bv.AMI_LEGACY


def test_redfish_kind_is_ami_legacy_vendor():
    out = probe_bmc_kind(
        "1.2.3.4", {"obmcuser": "u", "obmcpass": "p"}, [],
        port_probe=_ports(redfish=True),
        redfish_probe=lambda ip, creds: {"is_bmc": True,
                                         "product_name": "AMI Redfish",
                                         "redfish_version": "1.0.0"})
    assert out["kind"] == "redfish"
    assert out["vendor"] == _bv.AMI_LEGACY


def test_nothing_answers_is_unknown_vendor():
    out = probe_bmc_kind("1.2.3.4", {"obmcuser": "u", "obmcpass": "p"}, [],
                         port_probe=_ports())
    assert out["kind"] == "unknown"
    assert out["vendor"] == _bv.UNKNOWN


# ---------------------------------------------------------------------------
# FRU ID 0 only (spec 2026-09-14-fru-id0-only)
# ---------------------------------------------------------------------------
import pathlib as _pl

_FRU_FIX = _pl.Path(__file__).parent / "fixtures" / "fru"


def _fixture(name):
    return (_FRU_FIX / name).read_text()


def test_chassis_serial_traditional_reads_fru0_over_lan(monkeypatch):
    from flax_observe import bmc_probe
    calls = []
    monkeypatch.setattr(bmc_probe, "_default_ipmi_runner",
                        lambda ip, u, p, args: calls.append(args) or _fixture("wistron_three_block.txt"))
    assert bmc_probe.chassis_serial_traditional("10.0.0.1", ("U", "P"), family_map={}) == ("WTH49152ZGM1A", "ok")
    assert calls == [["fru", "print", "0"]]


def test_chassis_serial_traditional_nic_only_dump_is_absent_never_the_nic(monkeypatch):
    from flax_observe import bmc_probe
    calls = []
    monkeypatch.setattr(bmc_probe, "_sleep", lambda s: None)
    monkeypatch.setattr(bmc_probe, "_default_ipmi_runner",
                        lambda ip, u, p, args: calls.append(args) or _fixture("id0_absent_limit_nic_ava.txt"))
    assert bmc_probe.chassis_serial_traditional("10.0.0.1", ("U", "P"), family_map={}) == (None, "absent")
    assert len(calls) == 2                      # one retry while absent


def test_chassis_serial_traditional_absent_then_present_on_the_retry(monkeypatch):
    from flax_observe import bmc_probe
    reads = iter([_fixture("id0_absent_not_found_alone.txt"), _fixture("quanta_tioga_pass.txt")])
    monkeypatch.setattr(bmc_probe, "_sleep", lambda s: None)
    monkeypatch.setattr(bmc_probe, "_default_ipmi_runner", lambda ip, u, p, args: next(reads))
    assert bmc_probe.chassis_serial_traditional("10.0.0.1", ("U", "P"), family_map={}) == ("QCFF0818360193", "ok")


def test_chassis_serial_traditional_no_serial_and_error(monkeypatch):
    from flax_observe import bmc_probe
    monkeypatch.setattr(bmc_probe, "_default_ipmi_runner",
                        lambda ip, u, p, args: _fixture("wistron_tioga_pass_no_serial_et23b2.txt"))
    assert bmc_probe.chassis_serial_traditional("10.0.0.1", ("U", "P"), family_map={}) == (None, "no_serial")

    def dead(ip, u, p, args):
        raise RuntimeError("timeout")
    monkeypatch.setattr(bmc_probe, "_default_ipmi_runner", dead)
    assert bmc_probe.chassis_serial_traditional("10.0.0.1", ("U", "P"), family_map={}) == (None, "error")


def test_chassis_serial_leopard_uses_the_chassis_serial(monkeypatch):
    from flax_observe import bmc_probe
    from flax_discover.family_map import compile_family_map_text
    fm = {"leopard": compile_family_map_text("Leopard ORv2-DDR4\n")}
    monkeypatch.setattr(bmc_probe, "_default_ssh_runner", lambda *a, **k: _fixture("leopard_orv2.txt"))
    assert bmc_probe.chassis_serial_openbmc("10.0.0.1", ("root", "pw"), family_map=fm) == ("QCFF0617260650", "ok")


def test_chassis_serial_openbmc_empty_chain_output_is_absent(monkeypatch):
    from flax_observe import bmc_probe
    monkeypatch.setattr(bmc_probe, "_default_ssh_runner", lambda *a, **k: "")
    assert bmc_probe.chassis_serial_openbmc("10.0.0.1", ("root", "pw"), family_map={}) == (None, "absent")


def test_chassis_serial_uses_the_live_family_map_by_default(monkeypatch):
    from flax_observe import bmc_probe
    from flax_discover.family_map import compile_family_map_text
    monkeypatch.setattr(bmc_probe._fm_live, "current",
                        lambda path=None: {"leopard": compile_family_map_text("Leopard ORv2-DDR4\n")})
    monkeypatch.setattr(bmc_probe, "_default_ssh_runner", lambda *a, **k: _fixture("leopard_orv2.txt"))
    assert bmc_probe.chassis_serial_openbmc("10.0.0.1", ("root", "pw")) == ("QCFF0617260650", "ok")


def test_probe_ipmi_branch_reads_fru0_and_retries_once_while_absent(monkeypatch):
    from flax_observe import bmc_probe
    calls = []
    reads = iter([_fixture("id0_absent_limit_nic_ava.txt"), _fixture("quanta_tioga_pass.txt")])
    monkeypatch.setattr(bmc_probe, "_sleep", lambda s: None)

    def ipmi(ip, u, p, args):
        calls.append(args)
        return next(reads)

    out = probe_bmc_kind("1.2.3.4", {"obmcuser": "u", "obmcpass": "p"},
                         [{"bmcuser": "U", "bmcpass": "P"}],
                         ipmi_runner=ipmi, port_probe=lambda h: {"ssh": False, "ipmi": True, "redfish": False})
    assert out["kind"] == "traditional" and out["vendor"] == "ami_legacy"
    assert out["product_name"] == "Tioga Pass Single Side"
    assert calls == [["fru", "print", "0"], ["fru", "print", "0"]]


def test_probe_ipmi_branch_nic_only_gives_no_product_name(monkeypatch):
    from flax_observe import bmc_probe
    monkeypatch.setattr(bmc_probe, "_sleep", lambda s: None)
    out = probe_bmc_kind("1.2.3.4", {"obmcuser": "u", "obmcpass": "p"},
                         [{"bmcuser": "U", "bmcpass": "P"}],
                         ipmi_runner=lambda *a: _fixture("id0_absent_limit_nic_ava.txt"),
                         port_probe=lambda h: {"ssh": False, "ipmi": True, "redfish": False})
    assert out["kind"] == "traditional" and out["product_name"] is None   # never the NIC's name


# ---------------------------------------------------------------------------
# Tests for _default_ipmi_runner: cipher and sdr_cache parameters
# ---------------------------------------------------------------------------

def _capture_runs(monkeypatch, fail_first=False):
    import subprocess
    from flax_observe import ipmi
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if fail_first and len(calls) == 1:
            raise subprocess.CalledProcessError(1, argv, stderr=b"invalid authentication algorithm")
        return subprocess.CompletedProcess(argv, 0, stdout=b"Chassis Power is on\n", stderr=b"")
    monkeypatch.setattr(ipmi.subprocess, "run", fake_run)
    return ipmi, calls


def test_runner_with_cipher_makes_exactly_one_call(monkeypatch):
    ipmi, calls = _capture_runs(monkeypatch)
    ipmi._default_ipmi_runner("1.2.3.4", "root", "x", ["power", "status"], cipher=17)
    assert len(calls) == 1
    assert calls[0][:3] == ["ipmitool", "-C", "17"]
    assert "-U" in calls[0] and calls[0][calls[0].index("-U") + 1] == "root"


def test_runner_with_cipher_does_not_fall_back(monkeypatch):
    import subprocess, pytest
    ipmi, calls = _capture_runs(monkeypatch, fail_first=True)
    with pytest.raises(subprocess.CalledProcessError):
        ipmi._default_ipmi_runner("1.2.3.4", "root", "x", ["power", "status"], cipher=17)
    assert len(calls) == 1


def test_runner_without_cipher_keeps_3_then_auto(monkeypatch):
    ipmi, calls = _capture_runs(monkeypatch, fail_first=True)
    ipmi._default_ipmi_runner("1.2.3.4", "u", "p", ["power", "status"])
    assert calls[0][:3] == ["ipmitool", "-C", "3"]
    assert calls[1][1] != "-C"


def test_runner_sdr_cache_adds_S(monkeypatch):
    ipmi, calls = _capture_runs(monkeypatch)
    ipmi._default_ipmi_runner("1.2.3.4", "root", "x", ["exec", "f"], cipher=17,
                              sdr_cache="/run/flax/sdr-cache/aa.sdr")
    i = calls[0].index("-S")
    assert calls[0][i + 1] == "/run/flax/sdr-cache/aa.sdr"


# ---------------------------------------------------------------------------
# Task 4: bmc_probe delegates to sdr_cache; cipher support
# ---------------------------------------------------------------------------

def test_power_and_sdr_with_mac_delegates_to_sdr_cache(monkeypatch):
    from flax_observe import bmc_probe, sdr_cache
    seen = {}

    def fake(ip, login, mac, port, **kw):
        seen.update(ip=ip, login=login, mac=mac, port=port, cipher=kw.get("cipher"))
        return ("on", "141.60 W")
    monkeypatch.setattr(sdr_cache, "power_and_watts", fake)
    assert bmc_probe.bmc_power_and_sdr_traditional(
        "1.2.3.4", ("root", "x"), bmc_mac="aa:bb:cc:dd:ee:ff", port="et8b3", cipher=17) == ("on", "141.60 W")
    assert seen == {"ip": "1.2.3.4", "login": ("root", "x"), "mac": "aa:bb:cc:dd:ee:ff",
                    "port": "et8b3", "cipher": 17}


def test_chassis_serial_traditional_passes_cipher(monkeypatch):
    from flax_observe import bmc_probe
    ciphers = []

    def fake_runner(ip, u, p, args, cipher=None, **kw):
        ciphers.append(cipher)
        return "FRU Device Description : Builtin FRU Device (ID 0)\n Product Serial : SN1\n"
    monkeypatch.setattr(bmc_probe, "_default_ipmi_runner", fake_runner)
    bmc_probe.chassis_serial_traditional("1.2.3.4", ("USERID", "x"), cipher=3)
    assert ciphers and set(ciphers) == {3}


def test_default_ssh_runner_password_in_env_not_argv(monkeypatch):
    from flax_observe import bmc_probe
    seen = {}

    def fake(argv, **kw):
        seen.update(argv=argv, **kw)
        return ""
    monkeypatch.setattr(bmc_probe.subprocess, "check_output", fake)
    bmc_probe._default_ssh_runner("10.0.0.1", "root", "fake-bmc-pw", "true")
    assert seen["argv"][:2] == ["sshpass", "-e"]
    assert not any("fake-bmc-pw" in a for a in seen["argv"])
    assert seen["env"]["SSHPASS"] == "fake-bmc-pw"
