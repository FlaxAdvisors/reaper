from flax_post.nicd import service, manifest, config

# Mirror the real manifest: the OEM FB_ key AND the post-rebrand native MT_ key
# both map to the same image, so a card re-classifies (not -> unsupported) after
# --allow_psid_change flips its PSID from FB_2450111034 to MT_2450112034.
_M = {"FB_2450111034": {"dir": "MCX4411A-ACQN", "bin": "fw-x",
                        "target_version": "14.32.1010", "target_psid": "MT_2450112034"},
      "MT_2450112034": {"dir": "MCX4411A-ACQN", "bin": "fw-x",
                        "target_version": "14.32.1010", "target_psid": "MT_2450112034"}}

def _needs_update_row():
    return {"phase": "needs_update", "devices": [
        {"pci": "5e:00.0", "current": "14.20.1202", "psid": "FB_2450111034", "uefi": False,
         "secure": False, "target": "14.32.1010", "target_psid": "MT_2450112034", "phase": "needs_update"}]}

class FakeClock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def tick(self, dt): self.t += dt

class AutoClock:
    """Self-ticking fake clock: each call advances by `step` and returns the
    new time -- lets a test drive expiry deterministically without having to
    wrap deps.run/deps.bmc_run to tick a shared clock as a side effect."""
    def __init__(self, step=1.0, start=0.0):
        self.t = start
        self.step = step
        self.calls = 0
    def __call__(self):
        self.calls += 1
        self.t += self.step
        return self.t

class FakeDeps:
    def __init__(self, host_seq, redfish_ok=True, bmc_phase="up_to_date", bios_phase="up_to_date"):
        self.matcher = manifest.NicMatcher(_M)
        self.rows = {"et10b1": _needs_update_row()}
        self._host_seq = list(host_seq)     # successive (rc,out) for run()
        self._redfish_ok = redfish_ok
        self._bmc_phase, self._bios_phase = bmc_phase, bios_phase
        self.host_calls, self.redfish_calls = [], []
    def hosts(self): return [{"port": "et10b1", "host_ip": "1.2.3.4"}]
    def run(self, ip, script):
        self.host_calls.append(script)
        return self._host_seq.pop(0) if self._host_seq else (0, "")
    def redfish_bmc_reset(self, port):
        self.redfish_calls.append(port)
        return (True, "ok") if self._redfish_ok else (False, "Manager.Reset HTTP 500")
    def bmc_phase(self, port): return self._bmc_phase
    def bios_phase(self, port): return self._bios_phase
    def last_row(self, port): return self.rows.get(port, {})
    def set_row(self, port, **f): self.rows[port] = {**self.rows.get(port, {}), **f, "port": port}; return self.rows[port]

# post-flash the card re-queries at target+psid with UEFI ON:
_DONE_CARD = ("===CARD 5e:00.0===\nFW Version:  14.32.1010\nPSID:  MT_2450112034\n"
              "Security Attributes:  N/A\n===UEFI 5e:00.0===\n EXP_ROM_UEFI_x86_ENABLE True(1)\n")
# a card at target FW+PSID whose FW exposes NO EXP_ROM_UEFI_x86_ENABLE knob:
_NOUEFI_CARD = ("===CARD 5e:00.0===\nFW Version:  14.32.1010\nPSID:  MT_2450112034\n"
                "Security Attributes:  N/A\n===UEFI 5e:00.0===\n LEGACY_BOOT_PROTOCOL PXE(1)\n")

def test_enforce_flashes_and_reaches_up_to_date():
    # flash -> reset re-query(done) -> uefi-branch re-query(uefi ON) -> final query(uefi ON)
    deps = FakeDeps(host_seq=[(0, ""),           # flash_script burn+reset
                              (0, _DONE_CARD),    # wait_card_reset re-query -> matches
                              (0, _DONE_CARD),    # uefi-branch re-query -> uefi True (skip enable)
                              (0, _DONE_CARD)])   # final QUERY_SCRIPT -> up_to_date
    clock = FakeClock()
    service.enforce_once(deps, service.Registry(), "enforce", ["et10b1"], clock=clock)
    assert deps.rows["et10b1"]["phase"] == "up_to_date"

def test_enforce_faults_when_target_fw_lacks_uefi_knob():
    # After a successful flash the card is at target FW+PSID but its FW never
    # exposes EXP_ROM_UEFI_x86_ENABLE -> it can't UEFI-boot -> fault, not up_to_date.
    deps = FakeDeps(host_seq=[(0, ""),             # flash
                              (0, _NOUEFI_CARD),    # wait_card_reset -> FW+PSID match
                              (0, _NOUEFI_CARD)])   # uefi-branch re-query -> uefi None
    service.enforce_once(deps, service.Registry(), "enforce", ["et10b1"], clock=FakeClock())
    row = deps.rows["et10b1"]
    assert row["phase"] == "fault" and "uefi" in row["fault_reason"].lower()

def test_enforce_gate_blocks_when_bmc_not_ready():
    deps = FakeDeps(host_seq=[], bmc_phase="needs_update")
    service.enforce_once(deps, service.Registry(), "enforce", ["et10b1"], clock=FakeClock())
    assert deps.host_calls == []   # never flashed

def test_wait_card_reset_times_out_to_false():
    # The re-query keeps reporting the stale pre-flash version/psid, so it
    # never matches target; the (self-ticking) clock crosses the deadline
    # after a couple of polls and wait_card_reset must give up and return
    # False. REQUIRED assertion: False is returned once the deadline passes
    # without ever seeing a matching re-query.
    stale_card = ("===CARD 5e:00.0===\nFW Version: 14.20.1202\nPSID: FB_2450111034\n"
                  "Security Attributes: N/A\n===UEFI 5e:00.0===\n EXP_ROM_UEFI_x86_ENABLE False(0)\n")
    deps = FakeDeps(host_seq=[(0, stale_card)] * 10)
    entry = _M["FB_2450111034"]
    clock = AutoClock(step=2.0)  # ticks past the deadline after 3 calls
    assert service.wait_card_reset(deps, "1.2.3.4", "5e:00.0", entry, 5.0, clock) is False

def test_uefi_reset_never_takes_faults_the_node():
    # IMPORTANT regression test for the discarded UEFI-reset return value:
    # after a successful FW flash + reset, the card is re-queried and its
    # UEFI bit is still off, so _flash_node fires the uefi_script and calls
    # wait_card_reset again -- but that second wait never sees a matching
    # re-query (simulating a UEFI reset that never takes), so it must fault
    # the node with a uefi-mentioning fault_reason instead of silently
    # marching on with needbmcreset=True.
    flashed_uefi_off_card = (
        "===CARD 5e:00.0===\nFW Version:  14.32.1010\nPSID:  MT_2450112034\n"
        "Security Attributes:  N/A\n===UEFI 5e:00.0===\n EXP_ROM_UEFI_x86_ENABLE False(0)\n")
    stale_card = (
        "===CARD 5e:00.0===\nFW Version: 14.20.1202\nPSID: FB_2450111034\n"
        "Security Attributes: N/A\n===UEFI 5e:00.0===\n EXP_ROM_UEFI_x86_ENABLE False(0)\n")
    deps = FakeDeps(host_seq=[
        (0, ""),                       # flash_script burn+reset
        (0, flashed_uefi_off_card),    # 1st wait_card_reset poll -> FW/PSID match
        (0, flashed_uefi_off_card),    # post-flash re-query -> uefi still False
        (0, ""),                       # uefi_script command
        (0, stale_card),               # 2nd wait_card_reset poll #1 -> no match
        (0, stale_card),               # poll #2 -> no match
        (0, stale_card),               # poll #3 -> no match, then deadline trips
    ])
    clock = AutoClock(step=200.0)
    service.enforce_once(deps, service.Registry(), "enforce", ["et10b1"], clock=clock)
    row = deps.rows["et10b1"]
    assert row["phase"] == "fault"
    assert "uefi" in row["fault_reason"]


def test_uefi_toggle_never_resets_the_bmc():
    # Operator ruling 2026-10-08: enabling UEFI needs only the card reset. The
    # BMC reset was for enabling BMC use of the NIC, which nicd does not touch,
    # and a BMC reset under a running host garbled et24b2's serial console.
    uefi_off = ("===CARD 5e:00.0===\nFW Version:  14.32.1010\nPSID:  MT_2450112034\n"
                "Security Attributes:  N/A\n===UEFI 5e:00.0===\n EXP_ROM_UEFI_x86_ENABLE False(0)\n")
    deps = FakeDeps(host_seq=[
        (0, ""),            # flash_script burn+reset
        (0, uefi_off),      # wait_card_reset -> FW/PSID match
        (0, uefi_off),      # post-flash re-query -> uefi off
        (0, ""),            # uefi_script
        (0, _DONE_CARD),    # wait_card_reset after the UEFI change
        (0, _DONE_CARD),    # final re-query
    ])
    service.enforce_once(deps, service.Registry(), "enforce", ["et10b1"], clock=FakeClock())
    assert any("EXP_ROM_UEFI_x86_ENABLE=true" in c for c in deps.host_calls)   # the toggle DID run
    assert deps.rows["et10b1"]["phase"] == "up_to_date"
    assert deps.redfish_calls == []


