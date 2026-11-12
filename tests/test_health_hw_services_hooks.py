"""Plan 2026-11-09-site-health-board Task 5: bang-side hooks. bang_hw.sh reads in-band
IPMI (KCS: SDR, SEL, mc info) and dimmerr; services.sh reads docker and ss. Runs the
real scripts with the tools stubbed on PATH."""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOKS = ROOT / "roles/apply_health_metrics_hooks/files/health_hooks"

SDR = ("P0 Temp          | 29 degrees C      | ok\n"
       "QAVATPower       | no reading        | ns\n"
       "Fan 3            | 900 RPM           | nc\n"
       "P12V             | 13.90 Volts       | cr\n")
DIMM = ("B0        sn: 82239E2B       ce: 2    ue: 0\n"
        "A0        sn: 8271406A       ce: 0    ue: 1\n")


def _stub(bin_, name, body):
    p = bin_ / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)


def run(tmp_path, script, ipmi_ok=True, ack=None):
    bin_ = tmp_path / "bin"
    bin_.mkdir(exist_ok=True)
    rc = "0" if ipmi_ok else "1"
    _stub(bin_, "ipmitool", f"""[ {rc} = 0 ] || exit 1
case "$*" in
  *"mc info"*) echo "Device ID : 32";;
  *"sel info"*) echo "Entries          : 909";;
  *sdr*) printf '%s' '{SDR}';;
esac
""")
    _stub(bin_, "dimmerr", f"printf '%s' '{DIMM}'\n")
    _stub(bin_, "docker", """case "$*" in
  *"ps -a"*) printf 'prod-api-1 running\\nprod-bmc_fw-1 exited\\n';;
  *inspect*) case "$*" in *bmc_fw*) echo 3;; *) echo 0;; esac;;
esac
""")
    _stub(bin_, "ss", """printf 'udp UNCONN 0 0 172.29.0.10:53 0.0.0.0:*\\nudp UNCONN 0 0 172.28.0.10:53 0.0.0.0:*\\ntcp LISTEN 0 128 0.0.0.0:8443 0.0.0.0:*\\ntcp LISTEN 0 128 [::]:80 [::]:*\\n'
""")
    out = tmp_path / "tf"
    out.mkdir(exist_ok=True)
    ackf = tmp_path / "sel-acked"
    if ack is not None:
        ackf.write_text(f"{ack}\n")
    env = dict(os.environ, PATH=f"{bin_}:{os.environ['PATH']}", TEXTFILE_DIR=str(out),
               SEL_ACK_FILE=str(ackf), DIMMERR=str(bin_ / "dimmerr"))
    subprocess.run(["bash", str(HOOKS / script)], env=env, check=True)
    return (out / script.replace(".sh", ".prom")).read_text()


def test_bang_hw_sdr_sel_dimm(tmp_path):
    t = run(tmp_path, "bang_hw.sh", ack=900)
    assert "reaper_ipmi_kcs_ok 1" in t
    assert 'reaper_ipmi_sensor_ok{sensor="P0 Temp"} 1' in t
    assert 'reaper_ipmi_sensor_ok{sensor="QAVATPower"} 1' in t          # ns = no reading, not bad
    assert 'reaper_ipmi_sensor_ok{sensor="P12V"} 0' in t
    assert "reaper_sel_entries 909" in t and "reaper_sel_acked_entries 900" in t
    assert 'reaper_dimm_ce{slot="B0",serial="82239E2B"} 2' in t
    assert 'reaper_dimm_ue{slot="A0",serial="8271406A"} 1' in t


def test_bang_hw_kcs_down_still_writes(tmp_path):
    t = run(tmp_path, "bang_hw.sh", ipmi_ok=False)
    assert "reaper_ipmi_kcs_ok 0" in t and "reaper_ipmi_sensor_ok" not in t


def test_sel_ack_file_absent_is_seeded_with_the_current_count(tmp_path):
    # first run on a bang: today's 909 entries are history, not 909 "new"
    assert "reaper_sel_acked_entries 909" in run(tmp_path, "bang_hw.sh")
    assert (tmp_path / "sel-acked").read_text().strip() == "909"


def test_a_cleared_sel_resets_the_ack(tmp_path):
    # SEL cleared (909 -> fewer than acked): every entry since is new, and the stale ack
    # must not hide the next ones once the count climbs back past it
    t = run(tmp_path, "bang_hw.sh", ack=5000)
    assert "reaper_sel_acked_entries 0" in t and (tmp_path / "sel-acked").read_text().strip() == "0"


def test_non_critical_sensors_are_not_paged(tmp_path):
    t = run(tmp_path, "bang_hw.sh", ack=909)
    assert 'reaper_ipmi_sensor_ok{sensor="Fan 3"} 1' in t and 'reaper_ipmi_sensor_ok{sensor="P12V"} 0' in t


def test_services_containers_and_ports(tmp_path):
    t = run(tmp_path, "services.sh")
    assert 'reaper_container_running{name="prod-api-1"} 1' in t
    assert 'reaper_container_running{name="prod-bmc_fw-1"} 0' in t
    assert 'reaper_container_restarts{name="prod-bmc_fw-1"} 3' in t
    assert t.count('reaper_listening{proto="udp",port="53"} 1') == 1        # unique per port
    assert 'reaper_listening{proto="tcp",port="8443"} 1' in t
    assert 'reaper_listening{proto="tcp",port="80"} 1' in t
