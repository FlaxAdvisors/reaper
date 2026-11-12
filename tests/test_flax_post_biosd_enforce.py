from flax_post.biosd import service

class _D:
    def __init__(self, phase="needs_update", bmc="up_to_date"):
        self.row = {"port": "et6b1", "phase": phase, "current": "TPC_P25C",
                    "target": "TPC_P26C", "entry": {"flags": ["/X"], "afulnx_url": "u",
                                                     "bin_url": "b", "bin_path": "p"}}
        self._bmc = bmc; self.flashed = []
    def hosts(self): return [{"port": "et6b1", "host_ip": "1.2.3.4"}]
    def last_row(self, port): return self.row
    def bmc_phase(self, port): return self._bmc
    def run(self, ip, script): self.flashed.append(script); return 0, "ok"
    def set_row(self, port, **f): self.row.update(f); return self.row

def test_enforce_flashes_eligible_once():
    d = _D(); reg = service.Registry()
    service.enforce_once(d, reg, "enforce", [])
    assert len(d.flashed) == 1 and d.row["phase"] == "flashing" and reg.busy("et6b1")
    service.enforce_once(d, reg, "enforce", [])   # already busy -> no second flash
    assert len(d.flashed) == 1

def test_enforce_skips_when_bmc_not_ready():
    d = _D(bmc="needs_update"); reg = service.Registry()
    service.enforce_once(d, reg, "enforce", [])
    assert d.flashed == []

def test_enforce_skips_when_last_probe_unreachable():
    # A host whose last probe pass came back unreachable must not be flashed,
    # even fully allowlisted+enforce+bmc-ready -- ssh_ok is derived honestly
    # from the last-probe phase, not hardcoded True (regression for the
    # hardcoded-ssh_ok=True bug).
    d = _D(phase="unreachable"); reg = service.Registry()
    service.enforce_once(d, reg, "enforce", [])
    assert d.flashed == [] and not reg.busy("et6b1")

def test_enforce_skips_when_last_probe_unknown():
    d = _D(phase="unknown"); reg = service.Registry()
    service.enforce_once(d, reg, "enforce", [])
    assert d.flashed == [] and not reg.busy("et6b1")

def test_enforce_faults_and_releases_on_malformed_manifest_entry():
    # A manifest entry missing a required field (bin_path) makes
    # driver.flash_script raise KeyError before any ssh call happens; that
    # must surface as a fault and release the claim, not strand the port
    # "flashing" until FLASH_TIMEOUT_S nor crash the whole enforce pass.
    d = _D()
    d.row["entry"] = {"flags": ["/X"], "afulnx_url": "u", "bin_url": "b"}  # no bin_path
    reg = service.Registry()
    service.enforce_once(d, reg, "enforce", [])
    assert d.flashed == []
    assert d.row["phase"] == "fault"
    assert not reg.busy("et6b1")



def test_enforce_never_flashes_a_report_only_entry():
    d = _D(); d.row["entry"] = {"family": "leopard", "target": "F06_3B22", "updatable": False}
    service.enforce_once(d, service.Registry(), "enforce", [])
    assert d.flashed == [] and d.row["phase"] == "needs_update"
