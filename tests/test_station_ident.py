"""The station identifies the BLADE, never the card.

The BMC's MAC is allocated out of the MEZZANINE CARD's own MAC block, so it
changes every time a card is swapped -- which is this station's entire
purpose. Measured on bang-gouda 2026-09-22: et9b1 BMC 50:6b:4b:da:9d:ce with
flax's nodemac 50:6b:4b:da:9d:cc, et9b4 BMC ...f9:ea:50 with nodemac ...ea:4e.
That constant-2 is flax_switch_sense/classify.py's computed "phantom nic_mac",
and the "+1/+3 families" it fails on are NIC vendor OUIs -- because the offset
is a property of the CARD.

So the identity is the FRU ID 0 serial, picked by family: Chassis Serial on
leopard, Product Serial everywhere else, with NO fallback between the two
(operator ruling 2026-09-14).
"""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys

_REAPER_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PAYLOAD = _REAPER_ROOT / "scripts" / "unlock-with-ubuntu" / "payload"


sys.path.insert(0, str(_PAYLOAD))
import station_ident  # noqa: E402

# Verbatim from `ipmitool fru` on et9b1 (Leopard ORv2-DDR4), 2026-09-22.
FRU_LEOPARD = """FRU Device Description : Builtin FRU Device (ID 0)
 Chassis Type          : Rack Mount Chassis
 Chassis Serial        : QCFF06191403C6
 Board Mfg             : Quanta
 Board Product         : Leopard ORv2-DDR4
 Board Serial          : QTF4KQ90300005
 Board Part Number     : 31F06MB00N0
 Product Name          : Leopard ORv2-DDR4
 Product Part Number   : 1HY7UAZ0078

FRU Device Description : QAVABFRU (ID 1)
 Device not present (Requested sensor, data, or record not found)
"""

FRU_TIOGAPASS = """FRU Device Description : Builtin FRU Device (ID 0)
 Chassis Serial        : LOT-SHARED-NOT-A-BLADE-ID
 Board Product         : Tioga Pass Single Side
 Board Serial          : BOARDSN9999
 Product Name          : Tioga Pass Single Side
 Product Serial        : TPSN00012345
"""

# Verbatim from `ipmitool lan print 1` on et9b1, 2026-09-22.
LAN_PRINT = """Set in Progress         : Set Complete
IP Address Source       : DHCP Address
IP Address              : 172.16.9.101
Subnet Mask             : 255.255.0.0
MAC Address             : 50:6b:4b:da:9d:ce
Default Gateway IP      : 172.16.0.1
"""

FAMILY_MAP = {
    "leopard": ["Leopard-DDR3", "Leopard-DDR BW", "Leopard-Orv2",
                "Leopard ORv2-DDR4"],
    "tiogapass": ["^Tioga Pass( Single Side)?$"],
}


def _fm():
    import re
    return {k: [re.compile(p) for p in v] for k, v in FAMILY_MAP.items()}


def test_lan_print_yields_mac_and_ip():
    got = station_ident.parse_lan_print(LAN_PRINT)
    assert got["bmc_mac"] == "50:6b:4b:da:9d:ce"
    assert got["bmc_ip"] == "172.16.9.101"


def test_lan_print_does_not_mistake_the_source_line_for_the_address():
    # "IP Address Source : DHCP Address" sits directly above "IP Address".
    assert station_ident.parse_lan_print(LAN_PRINT)["bmc_ip"] != "DHCP"


def test_leopard_uses_chassis_serial():
    out = station_ident.build_ident(FRU_LEOPARD, LAN_PRINT, _fm(),
                                    "leopard9b1", "50:6b:4b:da:9d:cc")
    assert out["station_sn"] == "QCFF06191403C6"
    assert out["serial_field"] == "Chassis Serial"
    assert out["family"] == "leopard"


def test_tiogapass_uses_product_serial_not_chassis_serial():
    # Chassis Serial on Wistron is a LOT number shared across blades, which is
    # why the family picks the field instead of one rule fitting everything.
    out = station_ident.build_ident(FRU_TIOGAPASS, LAN_PRINT, _fm(),
                                    "tp1", "aa:bb:cc:dd:ee:ff")
    assert out["station_sn"] == "TPSN00012345"
    assert out["serial_field"] == "Product Serial"


def test_no_serial_is_reported_not_papered_over():
    # NO fallback between serial fields (operator ruling 2026-09-14): a
    # leopard with no Chassis Serial must NOT silently become Board Serial.
    fru = FRU_LEOPARD.replace(" Chassis Serial        : QCFF06191403C6\n", "")
    out = station_ident.build_ident(fru, LAN_PRINT, _fm(), "leopard9b1", "")
    assert out["state"] == "no_serial"
    assert not out["station_sn"]
    assert "QTF4KQ90300005" not in json.dumps(out)


def test_ident_carries_the_observed_card_mac_but_never_as_identity():
    out = station_ident.build_ident(FRU_LEOPARD, LAN_PRINT, _fm(),
                                    "leopard9b1", "50:6b:4b:da:9d:cc")
    assert out["host_mac"] == "50:6b:4b:da:9d:cc"
    assert out["station_sn"] == "QCFF06191403C6"   # unchanged by the card


def test_cli_exits_nonzero_on_no_serial(tmp_path):
    fru = FRU_LEOPARD.replace(" Chassis Serial        : QCFF06191403C6\n", "")
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "ipmitool").write_text(
        '#!/bin/sh\ncase "$*" in\n  fru) cat %s ;;\n  "lan print 1") cat %s ;;\nesac\n'
        % (tmp_path / "fru.txt", tmp_path / "lan.txt"))
    (fake / "ipmitool").chmod(0o755)
    (tmp_path / "fru.txt").write_text(fru)
    (tmp_path / "lan.txt").write_text(LAN_PRINT)
    fmdir = tmp_path / "family-map"
    fmdir.mkdir()
    (fmdir / "leopard.txt").write_text("Leopard ORv2-DDR4\n")
    env = dict(os.environ, PATH="%s:%s" % (fake, os.environ["PATH"]),
               MEZZ_FAMILY_MAP=str(fmdir))
    p = subprocess.run([sys.executable, str(_PAYLOAD / "station_ident.py")],
                       capture_output=True, text=True, env=env)
    assert p.returncode == 1, p.stderr
    assert "no_serial" in (p.stdout + p.stderr)


def test_the_real_file_on_disk_is_executable():
    """C1: station_ident.py shipped mode 100644 in git -- every executable
    else in payload/ is 100755, both call sites (deploy.sh, unlock_mellanox.sh)
    run it directly, and nothing chmods the bundle copy they actually invoke
    (deploy.sh only chmods the INSTALLED copy under /opt/flax/mezzflash).
    `cp -a` and tar both preserve the mode, so every deploy failed closed at
    "Permission denied", exit 126. Every OTHER test in this file imports the
    module directly or invokes it via `sys.executable`, so none of them
    exercise the real file's mode -- which is exactly why this got missed.
    Check the mode on disk directly instead."""
    real = _PAYLOAD / "station_ident.py"
    assert os.access(str(real), os.X_OK), (
        "%s is not executable (mode %o)" % (real, os.stat(real).st_mode & 0o777))


def test_missing_family_map_dir_is_fatal_not_a_silent_fallback(tmp_path):
    """I4: a missing FAMILY_MAP_DIR must not silently become the
    DEFAULT_SERIAL_FIELD ("Product Serial") fallback -- match_family({}, ...)
    always returns None, so on a platform carrying BOTH Chassis Serial and
    Product Serial the station would report a DIFFERENT serial than the
    fleet, with state == "ok", and flash happily: the exact "station and
    triage disagree about which blade is in a slot" failure the verbatim-copy
    machinery exists to prevent, and functionally the cross-field fallback
    the operator ruling forbids. family-map/ is build-generated and not
    committed, so this is hit on every checkout run."""
    fru_both_fields = FRU_LEOPARD.rstrip() + "\n Product Serial        : WRONGSERIAL999\n"
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "ipmitool").write_text(
        '#!/bin/sh\ncase "$*" in\n  fru) cat %s ;;\n  "lan print 1") cat %s ;;\nesac\n'
        % (tmp_path / "fru.txt", tmp_path / "lan.txt"))
    (fake / "ipmitool").chmod(0o755)
    (tmp_path / "fru.txt").write_text(fru_both_fields)
    (tmp_path / "lan.txt").write_text(LAN_PRINT)
    missing_fmdir = tmp_path / "no-such-family-map"
    env = dict(os.environ, PATH="%s:%s" % (fake, os.environ["PATH"]),
               MEZZ_FAMILY_MAP=str(missing_fmdir))
    p = subprocess.run([sys.executable, str(_PAYLOAD / "station_ident.py")],
                       capture_output=True, text=True, env=env)
    assert p.returncode == 1, (p.stdout, p.stderr)
    assert "family" in p.stderr.lower(), p.stderr
    # Must NOT silently succeed and report the wrong (fallback-field) serial.
    assert "WRONGSERIAL999" not in p.stdout, p.stdout
    assert "QCFF06191403C6" not in p.stdout, p.stdout


def test_empty_family_map_dir_is_also_fatal(tmp_path):
    """A directory that exists but loads no families (load_family_map_dir
    returns {}) is the same silent-fallback hazard as a missing directory."""
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "ipmitool").write_text(
        '#!/bin/sh\ncase "$*" in\n  fru) cat %s ;;\n  "lan print 1") cat %s ;;\nesac\n'
        % (tmp_path / "fru.txt", tmp_path / "lan.txt"))
    (fake / "ipmitool").chmod(0o755)
    (tmp_path / "fru.txt").write_text(FRU_LEOPARD)
    (tmp_path / "lan.txt").write_text(LAN_PRINT)
    empty_fmdir = tmp_path / "family-map"
    empty_fmdir.mkdir()
    env = dict(os.environ, PATH="%s:%s" % (fake, os.environ["PATH"]),
               MEZZ_FAMILY_MAP=str(empty_fmdir))
    p = subprocess.run([sys.executable, str(_PAYLOAD / "station_ident.py")],
                       capture_output=True, text=True, env=env)
    assert p.returncode == 1, (p.stdout, p.stderr)
    assert "family" in p.stderr.lower(), p.stderr


def test_the_fatal_message_is_reachable_under_set_eu(tmp_path):
    """C2: deploy.sh runs under `set -eu`. A plain assignment whose RHS is a
    command substitution takes THAT command's exit status, so `x=$(failing)`
    kills the script at -e before `ident_rc=$?` can even run -- the FATAL
    block (and the ident/stderr echoes before it) was dead code. Extract the
    real identity block out of deploy.sh and run it standalone, under the
    same `set -eu` deploy.sh itself uses, with a station_ident.py stub that
    fails -- proving the FATAL branch is genuinely reached rather than
    assumed reachable. Does not execute deploy.sh itself."""
    deploy = (_PAYLOAD / "deploy.sh").read_text()
    start = deploy.index('echo "== station identity =="')
    end = deploy.index('echo "== config =="')
    block = deploy[start:end]
    script = tmp_path / "snippet.sh"
    script.write_text("#!/bin/bash\nset -eu\nhere=%s\n%s\n" % (tmp_path, block))
    script.chmod(0o755)
    stub = tmp_path / "station_ident.py"
    stub.write_text(
        '#!/bin/sh\n'
        'echo \'{"station_sn":"","state":"no_serial"}\'\n'
        'echo "station_ident: no_serial -- boom" >&2\n'
        'exit 1\n')
    stub.chmod(0o755)
    p = subprocess.run(["bash", str(script)], capture_output=True, text=True)
    combined = p.stdout + p.stderr
    assert "FATAL: this blade cannot be a flash station." in combined, combined
    assert "Fault the slot and build another flash server." in combined, combined
    assert "boom" in combined, combined
    assert p.returncode == 1, (p.returncode, combined)


def test_deploy_installs_curl_so_pushing_is_possible():
    """I5: unlock_mellanox.sh pushes status via curl, but a deployed station
    has no network to fix this on afterwards -- deploy.sh installed
    mstflint/ipmitool/setserial but never curl, so every push returned 127
    and the reporting latch tripped silently, with no way to recover it."""
    deploy = (_PAYLOAD / "deploy.sh").read_text()
    install_lines = [l for l in deploy.splitlines() if "apt-get install" in l]
    assert install_lines, deploy
    assert any("curl" in l for l in install_lines), install_lines


def test_deploy_gates_on_identity_before_installing_anything():
    # "no_serial faults the slot and we build another FLASH server" (operator,
    # 2026-09-22). Catch it at COMMISSIONING -- deploy.sh is the one step that
    # already needs a network and is where a human is standing by -- so a
    # blade that cannot name itself never becomes a station at all, and no
    # tile ever has to represent one.
    deploy = (_PAYLOAD / "deploy.sh").read_text()
    assert "station_ident.py" in deploy
    assert "build another" in deploy.lower()
    # The gate must run BEFORE the units are installed, or a refused blade is
    # left half-configured and runs on its next boot.
    assert deploy.index("station_ident.py") < deploy.index("== service ==")
