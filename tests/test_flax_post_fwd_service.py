# tests/test_flax_post_fwd_service.py
"""flax_post.fwd.service: probe_once + /healthz control route."""
from fastapi.testclient import TestClient


import threading


class _Deps:
    def __init__(self, bmcs, client, rows):
        self._bmcs = bmcs; self._client = client; self.rows = rows
        self._lock = threading.Lock()
        from flax_post.fwd.manifest import Matcher
        self.matcher = Matcher({"tiogapass-openbmc": {
            "match": {"product_name": ["(?i)tioga"], "bmc_kind": "openbmc"},
            "target_version": "flax-onetree-1.0.6-202606261452",
            "check": {"method": "redfish-firmware-inventory", "id": "bmc_active"},
            "flash": {"method": "redfish-update-multipart", "artifact": "OpenBMC/fx.mtd.tar"},
            "auto": True}})
        self.share_base = "http://bang/share"
    def post_bmcs(self): return self._bmcs
    def client_for(self, bmc_ip): return self._client
    def fetch(self, base, rel): return b"IMG"
    def set_row(self, port, **f):
        with self._lock:
            self.rows.setdefault(port, {}).update(f); return self.rows[port]


class _Client:
    def __init__(self, **kw): self.kw = kw
    def get_product_name(self): return ("Tioga Pass", "ok")
    def get_power_state(self): return ("On", "ok")
    def get_fw_version(self, fw_id):
        return self.kw.get("version", ("flax-onetree-1.0.6-202606221107", "ok"))
    def post_flash(self, image, filename): return ("7", "ok")
    def poll_task(self, t): return (None, 0, "dark")


def test_probe_once_writes_needs_update():
    from flax_post.fwd import service
    rows = {}
    deps = _Deps([{"port": "et6b1", "kind": "bmc", "reservation_ip": "172.24.6.101"}], _Client(), rows)
    service.probe_once(deps)
    assert rows["et6b1"]["phase"] == "needs_update"
    assert rows["et6b1"]["bmc_ip"] == "172.24.6.101"


def test_probe_once_fans_out_over_all_bmcs():
    """The default (parallel) path must probe every BMC, not just the first."""
    from flax_post.fwd import service
    rows = {}
    bmcs = [{"port": "et%db1" % i, "kind": "bmc", "reservation_ip": "172.24.%d.101" % i}
            for i in range(1, 9)]
    deps = _Deps(bmcs, _Client(), rows)
    service.probe_once(deps)   # default workers -> ThreadPoolExecutor
    assert set(rows) == {b["port"] for b in bmcs}
    assert all(rows[p]["phase"] == "needs_update" for p in rows)


def test_probe_once_skips_port_mid_flash():
    """A port the registry marks in-flight must NOT be clobbered by a probe pass."""
    from flax_post.fwd import service
    rows = {"et6b1": {"phase": "monitoring"}}  # an in-flight flash already wrote this
    deps = _Deps([{"port": "et6b1", "kind": "bmc", "reservation_ip": "172.24.6.101"}], _Client(), rows)
    reg = service.FlashRegistry()
    reg.claim("et6b1")
    service.probe_once(deps, reg)
    assert rows["et6b1"]["phase"] == "monitoring"  # untouched — probe skipped it


def test_probe_once_sweeps_stale_rows_before_probing():
    """probe_once must call deps.sweep_stale with every CURRENT port (mid-flash
    ones included -- sweep cares about reservation existence, not flash state),
    before it does anything else."""
    from flax_post.fwd import service
    bmcs = [{"port": "et6b1", "mac": "aa", "reservation_ip": "172.24.6.101"},
            {"port": "et7b1", "mac": "bb", "reservation_ip": "172.24.7.101"}]
    rows = {}
    deps = _Deps(bmcs, _Client(), rows)
    seen = []
    deps.sweep_stale = lambda live_ports: seen.append(set(live_ports))
    service.probe_once(deps, workers=1)
    assert seen == [{"et6b1", "et7b1"}]
    assert "et6b1" in rows and "et7b1" in rows          # probing still happened


def test_probe_once_survives_sweep_stale_failure():
    """A broken sweep must not stop the probe pass itself."""
    from flax_post.fwd import service
    bmcs = [{"port": "et6b1", "mac": "aa", "reservation_ip": "172.24.6.101"}]
    rows = {}
    deps = _Deps(bmcs, _Client(), rows)

    def boom(live_ports):
        raise RuntimeError("store is on fire")

    deps.sweep_stale = boom
    service.probe_once(deps, workers=1)               # must not raise
    assert "et6b1" in rows


def test_probe_once_without_sweep_stale_hook_still_works():
    """Existing bare deps (no sweep_stale attribute at all) must keep working --
    the hook is optional, same discovery pattern as record_flash/record_action."""
    from flax_post.fwd import service
    bmcs = [{"port": "et6b1", "mac": "aa", "reservation_ip": "172.24.6.101"}]
    rows = {}
    deps = _Deps(bmcs, _Client(), rows)
    assert not hasattr(deps, "sweep_stale")
    service.probe_once(deps, workers=1)
    assert "et6b1" in rows


def test_flash_registry_claim_is_exclusive():
    from flax_post.fwd import service
    reg = service.FlashRegistry()
    assert reg.claim("et6b1") is True
    assert reg.is_flashing("et6b1") is True
    assert reg.claim("et6b1") is False           # already held
    reg.release("et6b1")
    assert reg.is_flashing("et6b1") is False
    assert reg.claim("et6b1") is True            # reclaimable after release


def test_healthz():
    from flax_post.fwd import service
    deps = _Deps([], _Client(), {})
    client = TestClient(service.build_app(deps))
    assert client.get("/healthz").json()["status"] == "ok"


def test_probe_endpoint_probes_one_port():
    rows = {}
    deps = _Deps([{"port": "et6b1", "reservation_ip": "10.0.0.1", "mac": "aa"}], _Client(), rows)
    from flax_post.fwd import service
    client = TestClient(service.build_app(deps, registry=service.FlashRegistry()))
    r = client.post("/probe/et6b1")
    assert r.status_code == 200 and r.json()["phase"] == "needs_update"
    assert rows["et6b1"]["phase"] == "needs_update"
    assert client.post("/probe/et9b9").status_code == 404


def test_probe_endpoint_skips_port_mid_flash():
    rows = {}
    deps = _Deps([{"port": "et6b1", "reservation_ip": "10.0.0.1", "mac": "aa"}], _Client(), rows)
    from flax_post.fwd import service
    reg = service.FlashRegistry(); reg.claim("et6b1")
    client = TestClient(service.build_app(deps, registry=reg))
    r = client.post("/probe/et6b1")
    assert r.status_code == 200 and r.json()["skipped"] == "flashing" and rows == {}


def test_family_for_vid_reads_vlans_json(tmp_path):
    import json
    from flax_post.fwd import manifest
    (tmp_path / "vlans.json").write_text(json.dumps([{"vid": 24, "family": "leopard"},
                                                     {"vid": 25, "family": "tiogapass"}]))
    assert manifest.family_for_vid(24, str(tmp_path)) == "leopard"
    assert manifest.family_for_vid(25, str(tmp_path)) == "tiogapass"
    assert manifest.family_for_vid(None, str(tmp_path)) is None
    assert manifest.family_for_vid(24, str(tmp_path / "nope")) is None


def test_enforce_skips_the_gauntlet_for_a_leopard(monkeypatch, tmp_path):
    import json
    from flax_post.fwd import service, manifest, config
    (tmp_path / "vlans.json").write_text(json.dumps([{"vid": 24, "family": "leopard"}]))
    monkeypatch.setattr(config, "CONFIG_DIR", str(tmp_path))
    rows = {}
    deps = _Deps([{"port": "et7b1", "kind": "bmc", "reservation_ip": "172.24.7.101", "vid": 24}],
                 _Client(), rows)
    deps.matcher = manifest.PostMatcher({"leopard-ami": {"match": {"product_name": ["(?i)leopard"]},
                                                         "updatable": False, "target_version": "14.00"}})
    ran = []

    class _Reg:
        def claim(self, p): return True
        def release(self, p): pass

    class _Ex:
        def submit(self, fn): fn()
    service.enforce_once(deps, _Reg(), _Ex(), mode="enforce", allowlist=None,
                         run_node=lambda *a, **k: ran.append(a) or "done")
    assert ran == [] and rows["et7b1"]["phase"] == "oem"
