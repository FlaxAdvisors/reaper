"""tpm_clear.sh against a fake PPI sysfs dir and a stub TPM-state command."""
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "tpm_clear.sh")

OWNED = "owned=1 perm=0x00000007 ownerAuthSet=1 endorsementAuthSet=1 lockoutAuthSet=1 disableClear=0 persistent=2"
CLEAN = "owned=0 perm=0x00000000 ownerAuthSet=0 endorsementAuthSet=0 lockoutAuthSet=0 disableClear=0 persistent=0"
OPS_UNATTENDED = "0 4: User not required\n5 4: User not required\n14 4: User not required\n96 3: User required\n"
OPS_NEEDS_USER = "0 4: User not required\n5 3: User required\n"


def _ppi(tmp, request="96", response="0: No Recent Request", ops=OPS_UNATTENDED):
    d = os.path.join(tmp, "ppi")
    os.makedirs(d)
    for name, val in (("request", request), ("response", response),
                      ("tcg_operations", ops), ("version", "1.3")):
        with open(os.path.join(d, name), "w") as f:
            f.write(val + "\n")
    return d


def _run(ppi, state, note_log=None, sce=(3, "SCE-SKIP reason=sce-test-default"), efivars=None,
         sce_enable=(3, "SCE-SKIP reason=sce-test-default")):
    # The SCE route is a stub: it logs that it ran and prints/exits as told.
    # efivars is the NV-variable dir that holds the "SCE clear queued" marker.
    base = os.path.dirname(ppi)
    efivars = efivars or os.path.join(base, "efivars")
    os.makedirs(efivars, exist_ok=True)
    sce_cmd = os.path.join(base, "sce_stub.sh")
    with open(sce_cmd, "w") as f:
        f.write("#!/bin/bash\necho ran >> %s/sce.calls\necho '%s'\nexit %d\n" % (base, sce[1], sce[0]))
    os.chmod(sce_cmd, 0o755)
    enable_cmd = os.path.join(base, "sce_enable_stub.sh")
    with open(enable_cmd, "w") as f:
        f.write("#!/bin/bash\necho ran >> %s/sce_enable.calls\necho '%s'\nexit %d\n"
                % (base, sce_enable[1], sce_enable[0]))
    os.chmod(enable_cmd, 0o755)
    env = dict(os.environ, TPM_PPI_DIR=ppi, TPM_STATE_CMD="echo " + state,
               TPM_SCE_CMD=sce_cmd, TPM_SCE_ENABLE_CMD=enable_cmd, TPM_EFIVARS_DIR=efivars)
    cmd = ["bash", SCRIPT]
    if note_log:   # post.sh exports ps_note; prove the progress notes reach it
        cmd = ["bash", "-c", 'ps_note() { echo "$*" >> %s; }; export -f ps_note; bash %s'
               % (note_log, SCRIPT)]
    p = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       universal_newlines=True, timeout=30)
    return p, open(os.path.join(ppi, "request")).read().strip()


def test_owned_tpm_queues_a_clear_and_exits_10(tmp_path):
    ppi = _ppi(str(tmp_path))
    p, req = _run(ppi, OWNED)
    assert p.returncode == 10 and "TPM-QUEUED" in p.stdout and req == "5"


def test_clean_tpm_is_left_alone(tmp_path):
    ppi = _ppi(str(tmp_path))
    p, req = _run(ppi, CLEAN)
    assert p.returncode == 0 and "TPM-CLEAN" in p.stdout and req == "96"


def test_second_pass_reports_the_bios_clear(tmp_path):
    ppi = _ppi(str(tmp_path), response="5 0: Success")
    p, req = _run(ppi, CLEAN)
    assert "TPM-CLEARED-AT-BOOT 5 0: Success" in p.stdout and "TPM-CLEAN" in p.stdout
    assert p.returncode == 0 and req == "96"


def test_no_loop_when_bios_left_the_request_pending(tmp_path):
    ppi = _ppi(str(tmp_path), request="5")
    p, _ = _run(ppi, OWNED)
    assert p.returncode == 0 and "reason=bios-left-the-previous-clear-pending" in p.stdout


def test_no_loop_when_still_owned_after_a_successful_clear(tmp_path):
    ppi = _ppi(str(tmp_path), response="5 0: Success")
    p, req = _run(ppi, OWNED)
    assert p.returncode == 0 and "reason=still-owned-after-a-successful-clear" in p.stdout and req == "96"


def test_no_loop_when_bios_refused(tmp_path):
    ppi = _ppi(str(tmp_path), response="5 1: BIOS failure")
    p, req = _run(ppi, OWNED)
    assert p.returncode == 0 and "reason=bios-refused-clear" in p.stdout and req == "96"


def test_never_queues_when_the_bios_would_prompt_at_the_console(tmp_path):
    ppi = _ppi(str(tmp_path), ops=OPS_NEEDS_USER)
    p, req = _run(ppi, OWNED)
    assert p.returncode == 0 and "reason=op5-needs-console-confirmation:3:" in p.stdout and req == "96"


def test_no_tpm_and_unreadable_tpm_are_skips(tmp_path):
    p = subprocess.run(["bash", SCRIPT], env=dict(os.environ, TPM_PPI_DIR=str(tmp_path / "none")),
                       stdout=subprocess.PIPE, universal_newlines=True)
    assert p.returncode == 0 and "TPM-SKIP reason=no-tpm-ppi" in p.stdout
    ppi = _ppi(str(tmp_path))
    p, req = _run(ppi, "owned=? error=TPM_rc=0x101")
    assert p.returncode == 0 and "TPM-SKIP reason=unreadable" in p.stdout and req == "96"


def test_progress_reaches_the_banner_through_ps_note(tmp_path):
    ppi = _ppi(str(tmp_path))
    notes = str(tmp_path / "notes")
    p, _ = _run(ppi, OWNED, note_log=notes)
    text = open(notes).read()
    assert "reading TPM ownership" in text and "queueing a BIOS clear" in text


# TPM 1.2 (Leopards, operator 2026-10-27). tpm_state.py reports it from sysfs as
# "owned=... tpm=1.2 ...". PPI op 22 (Enable + Activate + Clear + Enable +
# Activate) is preferred because op 5 alone leaves a 1.2 TPM disabled and
# deactivated; op 5 is the fallback; either needs "4: User not required".
OWNED12 = "owned=1 tpm=1.2 enabled=1 active=1"
CLEAN12 = "owned=0 tpm=1.2 enabled=1 active=1"
OPS12 = "0 4: User not required\n5 4: User not required\n22 4: User not required\n"
OPS12_22_NEEDS_USER = "0 4: User not required\n5 4: User not required\n22 3: User required\n"
OPS12_NONE = "5 3: User required\n22 3: User required\n"


def test_tpm12_owned_queues_op_22(tmp_path):
    p, req = _run(_ppi(str(tmp_path), ops=OPS12), OWNED12)
    assert p.returncode == 10 and "TPM-QUEUED op=22" in p.stdout and req == "22"


def test_tpm12_falls_back_to_op_5_when_22_needs_the_console(tmp_path):
    p, req = _run(_ppi(str(tmp_path), ops=OPS12_22_NEEDS_USER), OWNED12)
    assert p.returncode == 10 and "TPM-QUEUED op=5" in p.stdout and req == "5"


def test_tpm12_skips_when_no_clear_op_is_unattended_and_sce_cannot(tmp_path):
    p, req = _run(_ppi(str(tmp_path), ops=OPS12_NONE), OWNED12,
                  sce=(3, "SCE-SKIP reason=sce-unknown-hii:DEADBEEF"))
    assert p.returncode == 0 and "TPM-SKIP reason=sce-unknown-hii:DEADBEEF" in p.stdout and req == "96"
    assert "op22-needs-console-confirmation" in p.stdout
    assert os.listdir(str(tmp_path / "efivars")) == []


def test_tpm12_second_pass_reports_the_bios_clear(tmp_path):
    p, req = _run(_ppi(str(tmp_path), ops=OPS12, response="22 0: Success"), CLEAN12)
    assert "TPM-CLEARED-AT-BOOT 22 0: Success" in p.stdout and "TPM-CLEAN" in p.stdout
    assert p.returncode == 0 and req == "96"


def test_tpm12_no_loop_when_22_left_pending(tmp_path):
    p, _ = _run(_ppi(str(tmp_path), ops=OPS12, request="22"), OWNED12)
    assert p.returncode == 0 and "reason=bios-left-the-previous-clear-pending" in p.stdout


def test_tpm12_no_loop_when_22_refused_or_ineffective(tmp_path):
    p, _ = _run(_ppi(str(tmp_path / "a"), ops=OPS12, response="22 1: BIOS Failure"), OWNED12)
    assert p.returncode == 0 and "reason=bios-refused-clear" in p.stdout
    p, _ = _run(_ppi(str(tmp_path / "b"), ops=OPS12, response="22 0: Success"), OWNED12)
    assert p.returncode == 0 and "reason=still-owned-after-a-successful-clear" in p.stdout


def test_tpm2_still_queues_op_5_even_when_22_is_listed(tmp_path):
    p, req = _run(_ppi(str(tmp_path), ops=OPS12), OWNED)
    assert p.returncode == 10 and "TPM-QUEUED op=5" in p.stdout and req == "5"


# --- TPM 1.2 through the BIOS setup (AMI SCE, tpm_sce.py) ------------------
# Leopards: every PPI clear op needs the console, so the clear is scheduled in
# setup NVRAM instead. An NV EFI variable remembers it was queued, so the next
# pass can tell "cleared" from "the BIOS ignored it" and never queues twice.
MARKER = "FlaxTpmClear-7a1f5c2e-3b8d-4e6a-9c41-0f2d6b8e5a93"
OPS12_PROMPTS = "0 4: User not required\n5 3: User required\n6 4: User not required\n22 3: User required\n"
CLEARED12 = "owned=0 tpm=1.2 enabled=0 active=0"


def _marker(tmp):
    d = os.path.join(str(tmp), "efivars")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, MARKER), "wb") as f:
        f.write(b"\x07\x00\x00\x00sce")
    return d


def _sce_ran(tmp):
    return os.path.exists(os.path.join(str(tmp), "sce.calls"))


def test_tpm12_sce_clear_queued_sets_the_marker_and_exits_10(tmp_path):
    p, req = _run(_ppi(str(tmp_path), ops=OPS12_PROMPTS), OWNED12,
                  sce=(0, "SCE-QUEUED token=94 hii=492F03E4"))
    assert p.returncode == 10 and "TPM-QUEUED op=sce token=94 hii=492F03E4" in p.stdout
    with open(str(tmp_path / "efivars" / MARKER), "rb") as f:
        assert f.read(4) == b"\x07\x00\x00\x00"           # NV + BS + RT
    assert req == "96"                                    # PPI untouched


def test_tpm12_sce_failure_is_a_fail_and_leaves_no_marker(tmp_path):
    p, _ = _run(_ppi(str(tmp_path), ops=OPS12_PROMPTS), OWNED12,
                sce=(1, "SCE-FAIL reason=sce-import-rc13"))
    assert p.returncode == 0 and "TPM-FAIL reason=sce-import-rc13" in p.stdout
    assert os.listdir(str(tmp_path / "efivars")) == []


def test_tpm12_no_sce_without_a_marker_slot(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o555)
    try:
        p, _ = _run(_ppi(str(tmp_path), ops=OPS12_PROMPTS), OWNED12,
                    sce=(0, "SCE-QUEUED token=94 hii=492F03E4"), efivars=str(ro))
    finally:
        ro.chmod(0o755)
    if os.geteuid() == 0:
        return   # root ignores the mode bits; nothing to prove here
    assert p.returncode == 0 and "TPM-SKIP reason=efivar-marker-not-writable" in p.stdout
    assert not _sce_ran(tmp_path)


def test_tpm12_still_owned_after_sce_clear_fails_and_never_requeues(tmp_path):
    _marker(tmp_path)
    p, _ = _run(_ppi(str(tmp_path), ops=OPS12_PROMPTS), OWNED12,
                sce=(0, "SCE-QUEUED token=94 hii=492F03E4"))
    assert p.returncode == 0 and "TPM-FAIL reason=still-owned-after-sce-clear" in p.stdout
    assert not _sce_ran(tmp_path)
    assert os.path.exists(str(tmp_path / "efivars" / MARKER))   # kept: stays a FAIL


# Re-enable (et29b4 2026-10-28): after a 1.2 clear the BIOS syncs setup to
# "TPM State = Disabled" and re-applies it every POST, so PPI op 6 answers
# "6 0: Success" and changes nothing. The re-enable is SCE TPM State = Enabled
# (tpm_sce.py enable) + a reboot, remembered by its own EFI marker so a write
# that did not take is a TPM-FAIL, never a loop.
EMARKER = "FlaxTpmEnable-7a1f5c2e-3b8d-4e6a-9c41-0f2d6b8e5a93"
ENABLE_OK = (0, "SCE-QUEUED token=93 hii=492F03E4")


def _emarker(tmp):
    d = os.path.join(str(tmp), "efivars")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, EMARKER), "wb") as f:
        f.write(b"\x07\x00\x00\x00sce")


def _enable_ran(tmp):
    return os.path.exists(os.path.join(str(tmp), "sce_enable.calls"))


def _has(tmp, name):
    return os.path.exists(os.path.join(str(tmp), "efivars", name))


def test_tpm12_sce_cleared_and_disabled_queues_the_sce_enable(tmp_path):
    _marker(tmp_path)
    p, req = _run(_ppi(str(tmp_path), ops=OPS12_PROMPTS), CLEARED12, sce_enable=ENABLE_OK)
    assert "TPM-CLEARED-AT-BOOT sce" in p.stdout
    assert p.returncode == 10 and "TPM-QUEUED op=sce-enable token=93 hii=492F03E4" in p.stdout
    assert _has(tmp_path, EMARKER) and not _has(tmp_path, MARKER) and req == "96"


def test_tpm12_sce_cleared_and_enabled_is_clean(tmp_path):
    _marker(tmp_path)
    p, req = _run(_ppi(str(tmp_path), ops=OPS12_PROMPTS), CLEAN12, sce_enable=ENABLE_OK)
    assert p.returncode == 0 and "TPM-CLEARED-AT-BOOT sce" in p.stdout and "TPM-CLEAN" in p.stdout
    assert req == "96" and not _has(tmp_path, MARKER) and not _enable_ran(tmp_path)


def test_tpm12_enabled_after_the_sce_enable_is_clean(tmp_path):
    _emarker(tmp_path)
    p, req = _run(_ppi(str(tmp_path), ops=OPS12_PROMPTS), CLEAN12, sce_enable=ENABLE_OK)
    assert p.returncode == 0 and "TPM-ENABLED-AT-BOOT sce" in p.stdout and "TPM-CLEAN" in p.stdout
    assert not _has(tmp_path, EMARKER) and not _enable_ran(tmp_path)


def test_tpm12_sce_enable_that_did_not_take_fails_and_never_requeues(tmp_path):
    _emarker(tmp_path)
    p, req = _run(_ppi(str(tmp_path), ops=OPS12_PROMPTS), CLEARED12, sce_enable=ENABLE_OK)
    assert p.returncode == 0 and "TPM-FAIL reason=still-disabled-after-enable" in p.stdout
    assert not _enable_ran(tmp_path) and _has(tmp_path, EMARKER)       # kept: stays a FAIL


def test_tpm12_old_op_6_failure_is_healed_once_by_the_sce_enable(tmp_path):
    # et29b3 tonight: cleared, op 6 "6 0: Success", still disabled, no marker.
    p, req = _run(_ppi(str(tmp_path), ops=OPS12_PROMPTS, response="6 0: Success"), CLEARED12,
                  sce_enable=ENABLE_OK)
    assert p.returncode == 10 and "TPM-QUEUED op=sce-enable" in p.stdout and _has(tmp_path, EMARKER)


def test_tpm12_enabled_after_op_6_is_clean(tmp_path):
    p, req = _run(_ppi(str(tmp_path), ops=OPS12_PROMPTS, response="6 0: Success"), CLEAN12,
                  sce_enable=ENABLE_OK)
    assert p.returncode == 0 and "TPM-CLEAN" in p.stdout and req == "96" and not _enable_ran(tmp_path)


def test_tpm12_disabled_without_our_clear_is_left_alone(tmp_path):
    # An operator- or vendor-disabled TPM we did not clear is not ours to turn on.
    p, req = _run(_ppi(str(tmp_path), ops=OPS12_PROMPTS), CLEARED12, sce_enable=ENABLE_OK)
    assert p.returncode == 0 and "TPM-CLEAN" in p.stdout and req == "96" and not _enable_ran(tmp_path)


def test_tpm12_op5_clear_at_boot_also_gets_the_sce_enable(tmp_path):
    p, req = _run(_ppi(str(tmp_path), ops=OPS12_PROMPTS, response="5 0: Success"), CLEARED12,
                  sce_enable=ENABLE_OK)
    assert p.returncode == 10 and "TPM-QUEUED op=sce-enable" in p.stdout and req == "96"


def test_tpm12_sce_enable_skip_and_failure_leave_no_marker(tmp_path):
    _marker(tmp_path / "skip")
    p, _ = _run(_ppi(str(tmp_path / "skip"), ops=OPS12_PROMPTS), CLEARED12,
                sce_enable=(3, "SCE-SKIP reason=sce-setup-already-enabled"))
    assert p.returncode == 0 and "TPM-SKIP reason=sce-setup-already-enabled" in p.stdout
    assert not _has(tmp_path / "skip", EMARKER)
    _marker(tmp_path / "fail")
    p, _ = _run(_ppi(str(tmp_path / "fail"), ops=OPS12_PROMPTS), CLEARED12,
                sce_enable=(1, "SCE-FAIL reason=sce-import-rc13"))
    assert p.returncode == 0 and "TPM-FAIL reason=sce-import-rc13" in p.stdout
    assert not _has(tmp_path / "fail", EMARKER)


def test_tpm2_never_takes_the_sce_route(tmp_path):
    p, _ = _run(_ppi(str(tmp_path), ops=OPS_NEEDS_USER), OWNED,
                sce=(0, "SCE-QUEUED token=94 hii=492F03E4"))
    assert p.returncode == 0 and "reason=op5-needs-console-confirmation" in p.stdout
    assert not _sce_ran(tmp_path)
