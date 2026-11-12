# tests/test_flax_post_fwd_flasher.py
"""flax_post.fwd.flasher: power-on-gated flash state machine, driven by a fake Redfish client."""


class FakeClient:
    """Scriptable RedfishClient stand-in. Sequences let one method return changing values."""
    def __init__(self, product="Tioga Pass", power="On",
                 versions=None, task=("7", "ok"), polls=None, flash=None,
                 redfish_root=(False, None, None), bmc_ip="172.25.6.101"):
        self._product = product
        self._power = power
        self._versions = list(versions or [])
        self._task = task
        self._polls = list(polls or [])
        self._flash = flash
        self._redfish_root = redfish_root
        self.bmc_ip = bmc_ip
    def get_product_name(self): return (self._product, "ok")
    def get_power_state(self): return (self._power, "ok")
    def get_fw_version(self, fw_id):
        return self._versions.pop(0) if self._versions else (None, "dark")
    def get_redfish_root(self): return self._redfish_root
    def post_flash(self, image, filename): return self._flash or self._task
    def poll_task(self, task_id):
        return self._polls.pop(0) if self._polls else (None, 0, "dark")


_MANIFEST = {"tiogapass-openbmc": {
    "match": {"product_name": ["(?i)tioga"], "bmc_kind": "openbmc"},
    "target_version": "flax-onetree-1.0.6-202606261452",
    "check": {"method": "redfish-firmware-inventory", "id": "bmc_active"},
    "flash": {"method": "redfish-update-multipart", "artifact": "OpenBMC/fx-tp.mtd.tar"},
    "auto": True}}


_OEM_MANIFEST = dict(_MANIFEST, **{"tiogapass-ami-oem": {
    "match": {"bmc_kind": "redfish", "product_name": [r"(?i)ami\s*redfish", r"(?i)^wiwynn$"]},
    "updatable": False}})


def _matcher():
    from flax_post.fwd.manifest import Matcher
    return Matcher(_MANIFEST)


def _oem_matcher():
    from flax_post.fwd.manifest import Matcher
    return Matcher(_OEM_MANIFEST)


def _recorder():
    rows = {}
    def set_row(port, **f):
        rows.setdefault(port, {}).update(f); return rows[port]
    return rows, set_row


def test_probe_up_to_date():
    from flax_post.fwd import flasher
    rows, set_row = _recorder()
    c = FakeClient(versions=[("flax-onetree-1.0.6-202606261452", "ok")])
    phase = flasher.probe_one("et6b1", c, _matcher(), set_row)
    assert phase == "up_to_date"
    assert rows["et6b1"]["phase"] == "up_to_date"


def test_probe_needs_update():
    from flax_post.fwd import flasher
    rows, set_row = _recorder()
    c = FakeClient(versions=[("flax-onetree-1.0.6-202606221107", "ok")])
    assert flasher.probe_one("et6b1", c, _matcher(), set_row) == "needs_update"


def test_flash_refuses_powered_off_host():
    # power-on policy: an OFF host is refused (operator powers it on+idle first);
    # a powered-ON host proceeds (covered by test_flash_happy_path_to_done).
    from flax_post.fwd import flasher
    rows, set_row = _recorder()
    c = FakeClient(power="Off", versions=[("flax-onetree-1.0.6-202606221107", "ok")])
    phase = flasher.flash_one("et6b1", c, _matcher(),
                              fetch=lambda b, r: b"IMG", set_row=set_row,
                              share_base="http://bang/share", sleep=lambda s: None)
    assert phase == "fault"
    assert "power on" in rows["et6b1"]["fault_reason"].lower()


def test_flash_happy_path_to_done():
    from flax_post.fwd import flasher
    rows, set_row = _recorder()
    # current=old -> needs flash; after flash, BMC returns at target.
    c = FakeClient(
        power="On",
        versions=[("flax-onetree-1.0.6-202606221107", "ok"),   # checking (old)
                  (None, "dark"),                                # verify-wait: still dark
                  ("flax-onetree-1.0.6-202606261452", "ok")],    # BMC back at target
        task=("7", "ok"),
        polls=[("New", 50, "ok"), (None, 0, "dark")])           # climbs, then dark
    phase = flasher.flash_one("et6b1", c, _matcher(),
                              fetch=lambda b, r: b"IMG", set_row=set_row,
                              share_base="http://bang/share", sleep=lambda s: None,
                              max_wait_s=120, poll_s=0)
    assert phase == "done"
    assert rows["et6b1"]["phase"] == "done"
    assert rows["et6b1"]["percent"] == 100  # done always shows 100%


def test_flash_clears_entity_cache_before_flashing_and_records_outcome():
    """Triage parity: entity-cache clear runs after the power gate, before the
    flash POST, and its outcome is recorded on the row regardless of result."""
    from flax_post.fwd import flasher
    rows, set_row = _recorder()
    calls = []

    def fake_clear(ip):
        calls.append(ip)
        return (True, "")

    c = FakeClient(
        power="On", bmc_ip="172.25.9.101",
        versions=[("flax-onetree-1.0.6-202606221107", "ok"),
                  (None, "dark"),
                  ("flax-onetree-1.0.6-202606261452", "ok")],
        task=("7", "ok"),
        polls=[("New", 50, "ok"), (None, 0, "dark")])
    phase = flasher.flash_one("et9b1", c, _matcher(),
                              fetch=lambda b, r: b"IMG", set_row=set_row,
                              share_base="http://bang/share", sleep=lambda s: None,
                              max_wait_s=120, poll_s=0, clear_entity_cache=fake_clear)
    assert phase == "done"
    assert calls == ["172.25.9.101"]                    # called with THIS node's bmc_ip
    assert rows["et9b1"]["entity_cache_cleared"] is True
    assert rows["et9b1"]["entity_cache_reason"] == ""


def test_flash_proceeds_when_entity_cache_clear_fails():
    """Best-effort: a cache-clear failure must NOT block the flash -- the update
    matters more than a stale-sensor cache file (same trade-off as triage)."""
    from flax_post.fwd import flasher
    rows, set_row = _recorder()
    c = FakeClient(
        power="On",
        versions=[("flax-onetree-1.0.6-202606221107", "ok"),
                  (None, "dark"),
                  ("flax-onetree-1.0.6-202606261452", "ok")],
        task=("7", "ok"),
        polls=[("New", 50, "ok"), (None, 0, "dark")])
    phase = flasher.flash_one("et6b1", c, _matcher(),
                              fetch=lambda b, r: b"IMG", set_row=set_row,
                              share_base="http://bang/share", sleep=lambda s: None,
                              max_wait_s=120, poll_s=0,
                              clear_entity_cache=lambda ip: (False, "ssh_unreachable"))
    assert phase == "done"                              # still flashed
    assert rows["et6b1"]["entity_cache_cleared"] is False
    assert rows["et6b1"]["entity_cache_reason"] == "ssh_unreachable"


def test_flash_proceeds_when_entity_cache_clear_raises():
    """Even an unexpected exception from the dep must not escape flash_one --
    same blanket-catch discipline triage's worker.py uses at this call site."""
    from flax_post.fwd import flasher
    rows, set_row = _recorder()

    def boom(ip):
        raise RuntimeError("unexpected")

    c = FakeClient(
        power="On",
        versions=[("flax-onetree-1.0.6-202606221107", "ok"),
                  (None, "dark"),
                  ("flax-onetree-1.0.6-202606261452", "ok")],
        task=("7", "ok"),
        polls=[("New", 50, "ok"), (None, 0, "dark")])
    phase = flasher.flash_one("et6b1", c, _matcher(),
                              fetch=lambda b, r: b"IMG", set_row=set_row,
                              share_base="http://bang/share", sleep=lambda s: None,
                              max_wait_s=120, poll_s=0, clear_entity_cache=boom)
    assert phase == "done"
    assert rows["et6b1"]["entity_cache_cleared"] is False
    assert "unexpected" in rows["et6b1"]["entity_cache_reason"]


def test_flash_does_not_clear_entity_cache_when_host_powered_off():
    """The cache clear sits AFTER the power gate -- an off host never reaches
    it (and the gate already faults before any flash attempt)."""
    from flax_post.fwd import flasher
    rows, set_row = _recorder()
    calls = []
    c = FakeClient(power="Off", versions=[("flax-onetree-1.0.6-202606221107", "ok")])
    phase = flasher.flash_one("et6b1", c, _matcher(),
                              fetch=lambda b, r: b"IMG", set_row=set_row,
                              share_base="http://bang/share", sleep=lambda s: None,
                              clear_entity_cache=lambda ip: calls.append(ip) or (True, ""))
    assert phase == "fault"
    assert calls == []


def test_flash_waits_through_old_version_during_activation():
    """During activation the BMC keeps answering at the OLD version before it goes
    dark and returns at the new one. flash must wait for the version to CHANGE from
    the pre-flash baseline, not fault on the first (stale) read."""
    from flax_post.fwd import flasher
    rows, set_row = _recorder()
    c = FakeClient(
        power="On",
        versions=[("flax-onetree-1.0.6-202606221107", "ok"),   # checking (old baseline)
                  ("flax-onetree-1.0.6-202606221107", "ok"),    # activation-wait: STILL old
                  (None, "dark"),                                # BMC rebooting to activate
                  ("flax-onetree-1.0.6-202606261452", "ok")],    # returns at target
        task=("7", "ok"), polls=[(None, 0, "dark")])
    phase = flasher.flash_one("et6b1", c, _matcher(),
                              fetch=lambda b, r: b"IMG", set_row=set_row,
                              share_base="http://bang/share", sleep=lambda s: None,
                              max_wait_s=120, poll_s=0)
    assert phase == "done"
    assert rows["et6b1"]["phase"] == "done"


def test_flash_activation_timeout_is_unreachable():
    """If the new version never activates within max_wait_s, the BMC is hung/rebooting
    -> a distinct 'unreachable' state with a power-cycle hint, NOT a flash 'fault'."""
    from flax_post.fwd import flasher
    rows, set_row = _recorder()
    c = FakeClient(
        power="On",
        versions=[("flax-onetree-1.0.6-202606221107", "ok")],   # checking; then always dark
        task=("7", "ok"), polls=[(None, 0, "dark")])
    phase = flasher.flash_one("et6b1", c, _matcher(),
                              fetch=lambda b, r: b"IMG", set_row=set_row,
                              share_base="http://bang/share", sleep=lambda s: None,
                              max_wait_s=2, poll_s=0)
    assert phase == "unreachable"
    assert "power-cycle" in rows["et6b1"]["fault_reason"].lower()


def test_probe_dark_bmc_is_unreachable_preserving_version():
    """The probe must NOT clobber a succeeded flash with a 'fault' when the BMC is
    transiently dark: it marks 'unreachable' and PRESERVES the prior current_version
    (so a node that reached target shows 'unreachable' AT target, never 'fault')."""
    from flax_post.fwd import flasher
    rows, set_row = _recorder()
    set_row("et6b1", phase="done", current_version="flax-onetree-1.0.6-202606261452")
    c = FakeClient(versions=[(None, "[Errno 113] No route to host")])
    phase = flasher.probe_one("et6b1", c, _matcher(), set_row)
    assert phase == "unreachable"
    assert rows["et6b1"]["phase"] == "unreachable"
    assert rows["et6b1"]["current_version"] == "flax-onetree-1.0.6-202606261452"  # NOT clobbered


def test_flash_dark_bmc_at_checking_is_unreachable():
    """A BMC dark when the operator triggers the flash -> 'unreachable', not 'fault'."""
    from flax_post.fwd import flasher
    rows, set_row = _recorder()
    c = FakeClient(power="On", versions=[(None, "No route to host")])
    phase = flasher.flash_one("et6b1", c, _matcher(),
                              fetch=lambda b, r: b"IMG", set_row=set_row,
                              share_base="http://bang/share", sleep=lambda s: None)
    assert phase == "unreachable"
    assert "unreachable" in rows["et6b1"]["fault_reason"].lower()


def test_flash_version_mismatch_after_flash_is_fault():
    from flax_post.fwd import flasher
    rows, set_row = _recorder()
    c = FakeClient(
        power="On",
        versions=[("flax-onetree-1.0.6-202606221107", "ok"),   # checking
                  ("flax-onetree-9.9.9-209901010000", "ok")],   # came back WRONG
        task=("7", "ok"), polls=[(None, 0, "dark")])
    phase = flasher.flash_one("et6b1", c, _matcher(),
                              fetch=lambda b, r: b"IMG", set_row=set_row,
                              share_base="http://bang/share", sleep=lambda s: None,
                              max_wait_s=120, poll_s=0)
    assert phase == "fault"
    assert "mismatch" in rows["et6b1"]["fault_reason"].lower()


def test_flash_unmanaged_platform_is_fault():
    from flax_post.fwd import flasher
    rows, set_row = _recorder()
    c = FakeClient(product="SuperMicro X11", versions=[("x", "ok")])
    phase = flasher.flash_one("et6b1", c, _matcher(),
                              fetch=lambda b, r: b"IMG", set_row=set_row,
                              share_base="http://bang/share", sleep=lambda s: None)
    assert phase == "fault"
    assert "unmanaged" in rows["et6b1"]["fault_reason"].lower()


def test_flash_monitoring_timeout_is_fault():
    from flax_post.fwd import flasher
    rows, set_row = _recorder()
    # task NEVER goes dark and never hits 100 -> monitoring must time out
    c = FakeClient(
        power="On",
        versions=[("flax-onetree-1.0.6-202606221107", "ok")],   # checking: old
        task=("7", "ok"),
        polls=[("New", 50, "ok")] * 50)                          # always answers <100
    phase = flasher.flash_one("et6b1", c, _matcher(),
                              fetch=lambda b, r: b"IMG", set_row=set_row,
                              share_base="http://bang/share", sleep=lambda s: None,
                              max_wait_s=3, poll_s=0)
    assert phase == "fault"
    assert "monitoring" in rows["et6b1"]["fault_reason"].lower()


def test_flash_non_semver_current_version_is_flashed_not_faulted():
    """Triage parity: compare() cannot fail, so a version that is not semver is
    simply a version that differs from the target -- it is flashed, not faulted.
    Previously compare() raised ValueError and the node went to 'fault'."""
    from flax_post.fwd import flasher
    rows, set_row = _recorder()
    c = FakeClient(power="On", versions=[("garbage-not-a-version", "ok")])
    phase = flasher.flash_one("et6b1", c, _matcher(),
                              fetch=lambda b, r: b"IMG", set_row=set_row,
                              share_base="http://bang/share", sleep=lambda s: None)
    assert phase != "fault"
    assert "not-a-version" not in str(rows["et6b1"].get("fault_reason", ""))


def test_probe_any_difference_from_target_is_needs_update():
    """Triage parity: no ordering. A build that differs from the target -- in
    either direction -- needs an update. The old code mapped "not older" to
    up_to_date, so a node needing a flash reported as current."""
    from flax_post.fwd import flasher
    rows, set_row = _recorder()
    c = FakeClient(versions=[("flax-onetree-1.0.7-202601010000", "ok")])
    assert flasher.probe_one("et6b1", c, _matcher(), set_row) == "needs_update"


def test_probe_reachable_redfish_oem_board_is_oem_not_unreachable():
    """A reachable Redfish board that isn't onetree-flashable (the onetree
    firmware-inventory read fails) but matches an updatable:false OEM manifest
    entry -> terminal 'oem' (shows its Redfish version), NOT a false 'unreachable'."""
    from flax_post.fwd import flasher
    rows, set_row = _recorder()
    c = FakeClient(versions=[(None, "GET .../bmc_active HTTP 401")],  # onetree path fails
                   redfish_root=(True, "1.17.0", "Ami Redfish"))       # but it IS a Redfish BMC
    phase = flasher.probe_one("et25b2", c, _oem_matcher(), set_row)
    assert phase == "oem"
    assert rows["et25b2"]["phase"] == "oem"
    assert rows["et25b2"]["current_version"] == "1.17.0"
    assert not rows["et25b2"].get("fault_reason")


def test_probe_unreachable_when_not_a_redfish_bmc():
    """A dark board that is NOT a reachable Redfish service root (Leopard 443-closed)
    stays 'unreachable' — the OEM branch must not swallow a genuine no-answer."""
    from flax_post.fwd import flasher
    rows, set_row = _recorder()
    c = FakeClient(versions=[(None, "Connection refused")],
                   redfish_root=(False, None, None))
    assert flasher.probe_one("et10b2", c, _oem_matcher(), set_row) == "unreachable"


def test_probe_reachable_redfish_but_no_oem_entry_is_unreachable():
    """Reachable Redfish, but the product matches no updatable:false entry (e.g. a
    genuinely-dark onetree board) -> unreachable, not oem."""
    from flax_post.fwd import flasher
    rows, set_row = _recorder()
    c = FakeClient(versions=[(None, "dark")],
                   redfish_root=(True, "1.9.0", "Wiwynn Tioga Pass"))  # onetree, not OEM
    assert flasher.probe_one("et6b1", c, _oem_matcher(), set_row) == "unreachable"


def test_flash_does_not_refuse_a_higher_looking_version_as_a_downgrade():
    """Triage parity: there is no ordering, so there is no downgrade to refuse.
    The old branch faulted every node whose build stamp differed from an
    unstamped target -- 'if the strings differ, the image differs, so flash.'"""
    from flax_post.fwd import flasher
    rows, set_row = _recorder()
    c = FakeClient(power="On", versions=[("flax-onetree-1.0.7-202601010000", "ok")])
    phase = flasher.flash_one("et6b1", c, _matcher(),
                              fetch=lambda b, r: b"IMG", set_row=set_row,
                              share_base="http://bang/share", sleep=lambda s: None)
    assert "downgrade" not in str(rows["et6b1"].get("fault_reason", "")).lower()


def test_probe_vendor_stock_ami_root_on_managed_platform_is_needs_update():
    """et25b3 (2026-09-11): a Wiwynn TP whose onetree inventory 401s but whose
    unauthenticated root answers 'AMI Redfish Server' runs the vendor's stock
    BMC firmware. Not OEM (triage flashes it to onetree) -> needs_update with
    current 'vendor-stock', so the post gate passes in detect and the tile says why."""
    from flax_post.fwd import flasher
    rows, set_row = _recorder()
    from flax_post.fwd.manifest import Matcher
    c = FakeClient(versions=[(None, "GET .../bmc_active HTTP 401")],
                   redfish_root=(True, "1.5.0", "AMI Redfish Server"))
    # eindhoven's manifest has no OEM entry: the stock path must fire on its own
    phase = flasher.probe_one("et25b3", c, Matcher(_MANIFEST), set_row)
    assert phase == "needs_update"
    assert rows["et25b3"]["phase"] == "needs_update"
    assert rows["et25b3"]["current_version"] == "vendor-stock"
    assert rows["et25b3"]["target_version"]
    assert not rows["et25b3"].get("fault_reason")


def test_probe_openbmc_root_with_ami_oem_is_not_vendor_stock():
    """et7b4 (2026-09-12): a onetree/OpenBMC board whose onetree inventory 401s and
    whose UNAUTH root has no Product (Oem lists Ami+Intel+OpenBmc, so get_redfish_root
    synthesizes 'Ami Redfish') must NOT read as vendor-stock — only a real
    'AMI Redfish Server' root does. No OEM entry either -> unreachable, not stock."""
    from flax_post.fwd import flasher
    rows, set_row = _recorder()
    from flax_post.fwd.manifest import Matcher
    c = FakeClient(versions=[(None, "GET .../bmc_active HTTP 401")],
                   redfish_root=(True, "1.17.0", "Ami Redfish"))   # synthesized, not the real Product
    assert flasher.probe_one("et7b4", c, Matcher(_MANIFEST), set_row) == "unreachable"
    assert rows["et7b4"].get("current_version") != "vendor-stock"


# --- Leopard / AMI report-only (2026-11-06) ---------------------------------
_POST_MANIFEST = {
    "leopard-ami": {"match": {"product_name": ["(?i)leopard"]}, "updatable": False,
                    "target_version": "14.00", "check": {"method": "ipmi", "id": "ipmi_fw_rev"}},
    "tiogapass-openbmc": dict(_MANIFEST["tiogapass-openbmc"], target_version="flax-onetree-1.1.5"),
}


class _NoTouchClient:
    def __getattr__(self, name):
        raise AssertionError("client.%s called for a report-only BMC" % name)


def _post_matcher():
    from flax_post.fwd.manifest import PostMatcher
    return PostMatcher(_POST_MANIFEST)


def test_probe_one_leopard_family_is_report_only_without_redfish():
    from flax_post.fwd import flasher
    rows, set_row = _recorder()
    assert flasher.probe_one("et7b1", _NoTouchClient(), _post_matcher(), set_row,
                             family="leopard") == "oem"
    assert rows["et7b1"]["phase"] == "oem" and rows["et7b1"]["target_version"] == "14.00"
    assert rows["et7b1"]["current_version"] == "report-only"


def test_probe_one_tiogapass_family_still_probes_onetree():
    from flax_post.fwd import flasher
    rows, set_row = _recorder()
    c = FakeClient(versions=[("flax-onetree-1.1.4", "ok")])
    assert flasher.probe_one("et8b1", c, _post_matcher(), set_row,
                             family="tiogapass") == "needs_update"
    assert rows["et8b1"]["target_version"] == "flax-onetree-1.1.5"


def test_flash_one_never_flashes_a_leopard():
    from flax_post.fwd import flasher
    rows, set_row = _recorder()
    assert flasher.flash_one("et7b1", _NoTouchClient(), _post_matcher(), lambda *a: b"",
                             set_row, "http://bang/share", family="leopard") == "oem"
