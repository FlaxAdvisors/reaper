from flax_post.nicd import service, manifest

_M = {"FB_2450111034": {"dir": "MCX4411A-ACQN", "bin": "fw-x",
                        "target_version": "14.32.1010", "target_psid": "MT_2450112034"}}
_CARD_OUT = ("===CARD 5e:00.0===\nFW Version:  14.20.1202\nPSID:  FB_2450111034\n"
             "Security Attributes:  N/A\n===UEFI 5e:00.0===\n EXP_ROM_UEFI_x86_ENABLE False(0)\n")

class FakeDeps:
    def __init__(self, out=_CARD_OUT, rc=0):
        self.matcher = manifest.NicMatcher(_M)
        self._out, self._rc = out, rc
        self.rows = {}
    def hosts(self): return [{"port": "et10b1", "host_ip": "1.2.3.4"}]
    def run(self, ip, script): return self._rc, self._out
    def set_row(self, port, **f): self.rows[port] = {**self.rows.get(port, {}), **f, "port": port}; return self.rows[port]
    def bmc_phase(self, port): return "up_to_date"
    def bios_phase(self, port): return "up_to_date"
    def last_row(self, port): return self.rows.get(port, {})

def test_probe_writes_needs_update_with_device_list():
    d = FakeDeps()
    service.probe_once(d, workers=1)
    row = d.rows["et10b1"]
    assert row["phase"] == "needs_update"
    assert row["devices"][0]["psid"] == "FB_2450111034"
    assert row["devices"][0]["target_psid"] == "MT_2450112034"

def test_probe_unreachable_when_ssh_fails():
    d = FakeDeps(rc=1, out="")
    service.probe_once(d, workers=1)
    assert d.rows["et10b1"]["phase"] == "unreachable"

def test_probe_no_cards_is_up_to_date():
    d = FakeDeps(out="no devices")
    service.probe_once(d, workers=1)
    assert d.rows["et10b1"]["phase"] == "up_to_date" and d.rows["et10b1"]["devices"] == []

def test_registry_claim_release():
    r = service.Registry()
    assert r.claim("et10b1") is True and r.claim("et10b1") is False
    assert r.busy("et10b1") is True
    r.release("et10b1"); assert r.busy("et10b1") is False


def test_probe_port_probes_only_that_host():
    d = FakeDeps()
    out = service.probe_port(d, service.Registry(), d.hosts()[0]["port"])
    assert out is not None and "phase" in out
    assert service.probe_port(d, service.Registry(), "et9b9") is None


def test_probe_port_skips_claimed_port():
    d = FakeDeps(); reg = service.Registry(); port = d.hosts()[0]["port"]; reg.claim(port)
    assert service.probe_port(d, reg, port) == {"port": port, "skipped": "flashing"}


def test_probe_port_reports_failure_without_fabricating_a_row():
    d = FakeDeps(); port = d.hosts()[0]["port"]
    def _boom(ip, script):
        raise RuntimeError("ssh exploded")
    d.run = _boom
    assert service.probe_port(d, service.Registry(), port) == {
        "port": port, "ok": False, "reason": "probe failed; see daemon log"}
    assert d.rows == {}


def test_probe_pass_skips_powered_off_hosts():
    """Twin of the biosd test: an off host is not ssh-probed by the scan pass
    (ruling 2026-09-12), so its last real row survives the power-off."""
    d = FakeDeps()
    d.hosts = lambda: [{"port": "et10b1", "host_ip": "1.2.3.4"}, {"port": "et10b2", "host_ip": "1.2.3.5"}]
    d.powered_off_ports = lambda: {"et10b2"}
    service.probe_once(d, workers=1)
    assert set(d.rows) == {"et10b1"}


def test_probe_port_still_probes_a_powered_off_host_on_demand():
    d = FakeDeps()
    d.powered_off_ports = lambda: {"et10b1"}
    assert service.probe_port(d, service.Registry(), "et10b1")["phase"] == "needs_update"
