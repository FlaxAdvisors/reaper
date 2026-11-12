from flax_post.biosd import service

class _FakeDeps:
    def __init__(self):
        self.matcher = _M(); self.writes = []
        self._dmi = {"et6b1": "Wiwynn Tioga Pass"}
        self._rom = {"et6b1": "TPC_P25C"}
    def hosts(self): return [{"port": "et6b1", "host_ip": "172.25.10.1"}]
    def run(self, ip, script):
        # emulate: `dmidecode -t bios` -> BIOS Version; `dmidecode -t system` -> product
        if "dmidecode -t bios" in script: return 0, "Version: %s\n" % self._rom["et6b1"]
        if "dmidecode" in script: return 0, self._dmi["et6b1"]
        return 0, ""
    def bmc_phase(self, port): return "up_to_date"
    def set_row(self, port, **f): self.writes.append((port, f)); return f

class _M:
    def match(self, p): return {"target": "TPC_P26C", "dmi_match": "Tioga Pass",
                                "afulnx_url": "u", "bin_url": "b", "flags": ["/X"]}

def test_probe_writes_needs_update_no_flash():
    d = _FakeDeps(); service.probe_once(d)
    port, f = d.writes[-1]
    assert port == "et6b1"
    assert f["current"] == "TPC_P25C" and f["target"] == "TPC_P26C" and f["phase"] == "needs_update"

def test_probe_fans_out_across_hosts_when_parallel():
    # Multiple hosts + workers>1 forces the ThreadPoolExecutor path; every host
    # must still get a write (mirrors flax_post.fwd.service.probe_once).
    d = _FakeDeps()
    d.hosts = lambda: [{"port": "et6b1", "host_ip": "172.25.10.1"},
                       {"port": "et6b2", "host_ip": "172.25.10.2"}]
    service.probe_once(d, workers=4)
    assert {port for port, _ in d.writes} == {"et6b1", "et6b2"}

def test_probe_isolates_a_raising_host():
    # One host's ssh call blowing up must not stop the rest of the pass from
    # being probed and written (per-host isolation, mirroring fwd).
    d = _FakeDeps()
    d.hosts = lambda: [{"port": "et6b1", "host_ip": "172.25.10.1"},
                       {"port": "et6b2", "host_ip": "172.25.10.2"}]
    _orig_run = d.run
    def _run(ip, script):
        if ip == "172.25.10.2":
            raise RuntimeError("ssh boom")
        return _orig_run(ip, script)
    d.run = _run
    service.probe_once(d, workers=1)   # sequential path -- deterministic order
    ports = [port for port, _ in d.writes]
    assert ports == ["et6b1"]           # et6b2 raised and produced no write, no crash


def test_probe_port_probes_only_that_host():
    d = _FakeDeps()
    out = service.probe_port(d, service.Registry(), "et6b1")
    assert out["phase"] == "needs_update" and d.writes[-1][0] == "et6b1"
    assert service.probe_port(d, service.Registry(), "et9b9") is None


def test_probe_port_skips_claimed_port():
    d = _FakeDeps(); reg = service.Registry(); reg.claim("et6b1")
    assert service.probe_port(d, reg, "et6b1") == {"port": "et6b1", "skipped": "flashing"}
    assert d.writes == []


def test_probe_port_reports_failure_without_fabricating_a_row():
    d = _FakeDeps()
    def _boom(ip, script):
        raise RuntimeError("ssh exploded")
    d.run = _boom
    assert service.probe_port(d, service.Registry(), "et6b1") == {
        "port": "et6b1", "ok": False, "reason": "probe failed; see daemon log"}
    assert d.writes == []


def test_probe_pass_skips_powered_off_hosts():
    """A powered-off host cannot answer ssh; probing it only overwrites the
    last real classification with `unreachable` (ruling 2026-09-12). The scan
    pass leaves it alone; deps without the hook behave as before."""
    d = _FakeDeps()
    d.hosts = lambda: [{"port": "et6b1", "host_ip": "172.25.10.1"},
                       {"port": "et6b2", "host_ip": "172.25.10.2"}]
    d.powered_off_ports = lambda: {"et6b2"}
    service.probe_once(d, workers=1)
    assert [port for port, _ in d.writes] == ["et6b1"]


def test_probe_port_still_probes_a_powered_off_host_on_demand():
    d = _FakeDeps()
    d.powered_off_ports = lambda: {"et6b1"}
    assert service.probe_port(d, service.Registry(), "et6b1")["phase"] == "needs_update"


def test_probe_unmatched_platform_is_a_fault_with_the_version_read():
    """A DMI product the manifest does not name is a FAILED check, not a pass
    (operator ruling 2026-10-06; et24b1 read `TiogaPass` and sailed through as
    `unsupported`). The BIOS version is still read so the tile shows it."""
    d = _FakeDeps()
    d.matcher = type("NoMatch", (), {"match": staticmethod(lambda p: None)})()
    d._dmi["et6b1"] = "System Information\n\tManufacturer: Wiwynn\n\tProduct Name: TiogaPass\n"
    service.probe_once(d)
    port, f = d.writes[-1]
    assert f["phase"] == "fault"
    assert f["current"] == "TPC_P25C" and f["target"] is None
    assert "TiogaPass" in f["fault_reason"] and "manifest" in f["fault_reason"]
    assert "entry" not in f                      # nothing for enforce_once to flash with



# --- Leopard parity with triage bios_fw (2026-11-06) ------------------------
_LEO = {"platform": "Quanta Leopard", "family": "leopard", "target": "F06_3B22",
        "updatable": False}


def _leo_deps(rom="F06_3B22", dmi_rc=0):
    d = _FakeDeps()
    d._rom["et6b1"] = rom
    d.matcher = type("M", (), {"match": lambda self, p: _LEO})()
    if dmi_rc:
        d.run = lambda ip, script: (dmi_rc, "")
    return d


def test_leopard_at_target_reads_up_to_date_against_f06():
    d = _leo_deps(); service.probe_once(d)
    _, f = d.writes[-1]
    assert (f["phase"], f["current"], f["target"]) == ("up_to_date", "F06_3B22", "F06_3B22")


def test_leopard_off_target_is_reported_never_marked_flashable():
    d = _leo_deps(rom="F06_3A10"); service.probe_once(d)
    _, f = d.writes[-1]
    assert f["target"] == "F06_3B22" and f["phase"] == "needs_update"
    assert f["entry"]["updatable"] is False


def test_unreachable_probe_clears_a_departed_blades_entry():
    d = _leo_deps(dmi_rc=255); service.probe_once(d)
    _, f = d.writes[-1]
    assert f["phase"] == "unreachable" and f["entry"] is None
