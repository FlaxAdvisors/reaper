"""tpm_clear.sh against a fake TPM sysfs dir, stub TPM-state + SCE commands and
a scratch EFI variable dir. Goal (operator 2026-10-29): on, cleared, enabled,
from whatever state the TPM arrives in; one BIOS step per boot."""
import os
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "tpm_clear.sh")
GUID = "7a1f5c2e-3b8d-4e6a-9c41-0f2d6b8e5a93"
MARKER = "FlaxTpmClear-" + GUID
EMARKER = "FlaxTpmEnable-" + GUID
OMARKER = "FlaxTpmOn-" + GUID

# TPM 2.0 (TiogaPass). tcg_operations as the TP BIOS lists them (et-series 10-26).
OWNED = "owned=1 perm=0x00000007 ownerAuthSet=1 endorsementAuthSet=1 lockoutAuthSet=1 disableClear=0 persistent=2"
CLEAN = "owned=0 perm=0x00000000 ownerAuthSet=0 endorsementAuthSet=0 lockoutAuthSet=0 disableClear=0 persistent=0"
ON2 = " startup=0x80000006 shEnable=1 ehEnable=1 phEnable=0"
OFF2 = " startup=0x80000000 shEnable=0 ehEnable=0 phEnable=0"
OPS = "0 4: User not required\n1 4: User not required\n5 4: User not required\n14 4: User not required\n96 3: User required\n"
OPS_5_PROMPTS = "0 4: User not required\n1 4: User not required\n5 3: User required\n"
OPS_1_PROMPTS = "0 4: User not required\n1 3: User required\n5 4: User not required\n"

# TPM 1.2 (Leopards): every PPI clear op prompts; op 6 is listed but ignored.
OWNED12 = "owned=1 tpm=1.2 enabled=1 active=1"
CLEAN12 = "owned=0 tpm=1.2 enabled=1 active=1"
OFF12 = "owned=0 tpm=1.2 enabled=0 active=0"
OWNED_OFF12 = "owned=1 tpm=1.2 enabled=0 active=0"
OPS12 = "0 4: User not required\n5 4: User not required\n22 4: User not required\n"
OPS12_22_PROMPTS = "0 4: User not required\n5 4: User not required\n22 3: User required\n"
OPS12_PROMPTS = "0 4: User not required\n5 3: User required\n6 4: User not required\n22 3: User required\n"

SCE_OK = (0, "SCE-QUEUED token=94 bios=F06_3B22 hii=492F03E4")
ENABLE_OK = (0, "SCE-QUEUED token=93 bios=F06_3B22 hii=492F03E4")
ON_OK = (0, "SCE-QUEUED token=91,92,95 bios=F06_3B22 hii=492F03E4")
SKIP = (3, "SCE-SKIP reason=sce-test-default")


def _tpm(tmp, request="96", response="0: No Recent Request", ops=OPS, ppi=True, tpm=True):
    """Fake /sys/class/tpm/tpm0 (+ ppi/). Returns the tpm0 path."""
    d = os.path.join(str(tmp), "tpm0")
    if not tpm:
        return d
    os.makedirs(d)
    if ppi:
        os.makedirs(os.path.join(d, "ppi"))
        for name, val in (("request", request), ("response", response),
                          ("tcg_operations", ops), ("version", "1.3")):
            with open(os.path.join(d, "ppi", name), "w") as f:
                f.write(val + "\n")
    return d


def _mark(tmp, name, how="sce", age=60, epoch=True):
    d = os.path.join(str(tmp), "efivars")
    os.makedirs(d, exist_ok=True)
    data = how + (" %d" % (time.time() - age) if epoch else "")
    with open(os.path.join(d, name), "wb") as f:
        f.write(b"\x07\x00\x00\x00" + data.encode())


def _marker_data(tmp, name):
    with open(os.path.join(str(tmp), "efivars", name), "rb") as f:
        return f.read()


def _has(tmp, name):
    return os.path.exists(os.path.join(str(tmp), "efivars", name))


def _ran(tmp, which):
    return os.path.exists(os.path.join(str(tmp), which + ".calls"))


def _stub(tmp, name, result):
    path = os.path.join(str(tmp), name + "_stub.sh")
    with open(path, "w") as f:
        f.write("#!/bin/bash\necho ran >> %s/%s.calls\necho '%s'\nexit %d\n"
                % (tmp, name, result[1], result[0]))
    os.chmod(path, 0o755)
    return path


def _run(tmp, tpm, state, sce=SKIP, sce_enable=SKIP, sce_on=SKIP, efivars=None, note_log=None):
    tmp = str(tmp)
    efivars = efivars or os.path.join(tmp, "efivars")
    os.makedirs(efivars, exist_ok=True)
    env = dict(os.environ, TPM_SYSFS_DIR=tpm, TPM_STATE_CMD="echo " + state,
               TPM_SCE_CMD=_stub(tmp, "sce", sce), TPM_SCE_ENABLE_CMD=_stub(tmp, "sce_enable", sce_enable),
               TPM_SCE_ON_CMD=_stub(tmp, "sce_on", sce_on), TPM_EFIVARS_DIR=efivars)
    cmd = ["bash", SCRIPT]
    if note_log:   # post.sh exports ps_note; prove the progress notes reach it
        cmd = ["bash", "-c", 'ps_note() { echo "$*" >> %s; }; export -f ps_note; bash %s' % (note_log, SCRIPT)]
    p = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       universal_newlines=True, timeout=30)
    try:
        req = open(os.path.join(tpm, "ppi", "request")).read().strip()
    except OSError:
        req = None
    return p, req


# --- TPM 2.0: clear --------------------------------------------------------

def test_owned_tpm2_queues_op_5_with_a_marker(tmp_path):
    p, req = _run(tmp_path, _tpm(tmp_path), OWNED + ON2)
    assert p.returncode == 10 and "TPM-QUEUED op=5 " in p.stdout and req == "5"
    assert _marker_data(tmp_path, MARKER).startswith(b"\x07\x00\x00\x00ppi5 ")


def test_clean_enabled_tpm2_is_left_alone(tmp_path):
    p, req = _run(tmp_path, _tpm(tmp_path), CLEAN + ON2)
    assert p.returncode == 0 and "TPM-CLEAN" in p.stdout and req == "96"
    assert os.listdir(str(tmp_path / "efivars")) == []          # probe cleaned up too


def test_tpm2_clear_confirmed_on_the_next_pass(tmp_path):
    _mark(tmp_path, MARKER, "ppi5")
    p, req = _run(tmp_path, _tpm(tmp_path, response="5 0: Success"), CLEAN + ON2)
    assert p.returncode == 0 and "TPM-CLEARED-AT-BOOT 5 0: Success" in p.stdout and "TPM-CLEAN" in p.stdout
    assert not _has(tmp_path, MARKER) and req == "96"


def test_stale_success_response_does_not_block_a_new_clear(tmp_path):
    # Cleared weeks ago ("5 0" kept by the BIOS), re-imaged since: clear again.
    p, req = _run(tmp_path, _tpm(tmp_path, response="5 0: Success"), OWNED + ON2)
    assert p.returncode == 10 and "TPM-QUEUED op=5 " in p.stdout and req == "5"
    assert "CLEARED-AT-BOOT" not in p.stdout


def test_tpm2_still_owned_after_our_clear_fails_and_never_requeues(tmp_path):
    _mark(tmp_path, MARKER, "ppi5")
    p, req = _run(tmp_path, _tpm(tmp_path, response="5 0: Success"), OWNED + ON2)
    assert p.returncode == 0 and "TPM-FAIL reason=still-owned-after-clear" in p.stdout
    assert _has(tmp_path, MARKER) and req == "96"


def test_tpm2_refused_clear_names_the_bios_answer(tmp_path):
    _mark(tmp_path, MARKER, "ppi5")
    p, _ = _run(tmp_path, _tpm(tmp_path, response="5 1: BIOS failure"), OWNED + ON2)
    assert p.returncode == 0 and "TPM-FAIL reason=bios-refused:5_1:_BIOS_failure" in p.stdout


def test_tpm2_clear_left_pending_fails(tmp_path):
    _mark(tmp_path, MARKER, "ppi5")
    p, _ = _run(tmp_path, _tpm(tmp_path, request="5"), OWNED + ON2)
    assert p.returncode == 0 and "TPM-FAIL reason=bios-left-the-request-pending:op5" in p.stdout


def test_a_failed_step_is_retried_on_a_later_visit(tmp_path):
    _mark(tmp_path, MARKER, "ppi5", age=3 * 3600)
    p, req = _run(tmp_path, _tpm(tmp_path, response="5 0: Success"), OWNED + ON2)
    assert "from an earlier visit dropped" in p.stdout
    assert p.returncode == 10 and "TPM-QUEUED op=5 " in p.stdout and req == "5"


def test_a_marker_without_an_epoch_is_judged_then_restamped(tmp_path):
    _mark(tmp_path, MARKER, "sce", epoch=False)
    p, _ = _run(tmp_path, _tpm(tmp_path, ops=OPS12_PROMPTS), OWNED12, sce=SCE_OK)
    assert p.returncode == 0 and "TPM-FAIL reason=still-owned-after-clear" in p.stdout
    assert not _ran(tmp_path, "sce")
    data = _marker_data(tmp_path, MARKER)[4:].split()
    assert data[0] == b"sce" and abs(int(data[1]) - time.time()) < 60


def test_tpm2_clear_that_would_prompt_is_a_fail(tmp_path):
    p, req = _run(tmp_path, _tpm(tmp_path, ops=OPS_5_PROMPTS), OWNED + ON2)
    assert p.returncode == 0 and "TPM-FAIL reason=owned:op5-needs-console-confirmation:3:" in p.stdout
    assert req == "96" and not _has(tmp_path, MARKER)


def test_tpm2_request_not_accepted_drops_the_marker(tmp_path):
    tpm = _tpm(tmp_path)
    os.chmod(os.path.join(tpm, "ppi", "request"), 0o444)
    p, _ = _run(tmp_path, tpm, OWNED + ON2)
    if os.geteuid() == 0:
        return   # root ignores the mode bits
    assert p.returncode == 0 and "TPM-FAIL reason=ppi-request-not-accepted:op5" in p.stdout
    assert not _has(tmp_path, MARKER)


def test_tpm2_without_ppi_cannot_clear_and_says_so(tmp_path):
    p, _ = _run(tmp_path, _tpm(tmp_path, ppi=False), OWNED + ON2)
    assert p.returncode == 0 and "TPM-FAIL reason=owned:no-ppi" in p.stdout


# --- TPM 2.0: enable -------------------------------------------------------

def test_tpm2_disabled_after_our_clear_queues_op_1(tmp_path):
    _mark(tmp_path, MARKER, "ppi5")
    p, req = _run(tmp_path, _tpm(tmp_path, response="5 0: Success"), CLEAN + OFF2)
    assert "TPM-CLEARED-AT-BOOT 5 0: Success" in p.stdout
    assert p.returncode == 10 and "TPM-QUEUED op=1 " in p.stdout and req == "1"
    assert not _has(tmp_path, MARKER) and _has(tmp_path, EMARKER)


def test_tpm2_disabled_without_our_clear_is_enabled_too(tmp_path):
    p, req = _run(tmp_path, _tpm(tmp_path), CLEAN + OFF2)
    assert p.returncode == 10 and "TPM-QUEUED op=1 " in p.stdout and req == "1"


def test_tpm2_owned_and_disabled_enables_first(tmp_path):
    p, req = _run(tmp_path, _tpm(tmp_path), OWNED + OFF2)
    assert p.returncode == 10 and "TPM-QUEUED op=1 " in p.stdout and req == "1"


def test_tpm2_enable_confirmed_then_the_clear_follows(tmp_path):
    _mark(tmp_path, EMARKER, "ppi1")
    p, req = _run(tmp_path, _tpm(tmp_path, response="1 0: Success"), OWNED + ON2)
    assert "TPM-ENABLED-AT-BOOT 1 0: Success" in p.stdout and not _has(tmp_path, EMARKER)
    assert p.returncode == 10 and "TPM-QUEUED op=5 " in p.stdout and req == "5"


def test_tpm2_still_disabled_after_op_1_fails_and_never_requeues(tmp_path):
    _mark(tmp_path, EMARKER, "ppi1")
    p, req = _run(tmp_path, _tpm(tmp_path, response="1 0: Success"), CLEAN + OFF2)
    assert p.returncode == 0 and "TPM-FAIL reason=still-disabled-after-enable" in p.stdout
    assert req == "96" and _has(tmp_path, EMARKER)


def test_tpm2_enable_that_would_prompt_is_a_fail(tmp_path):
    p, req = _run(tmp_path, _tpm(tmp_path, ops=OPS_1_PROMPTS), CLEAN + OFF2)
    assert p.returncode == 0 and "TPM-FAIL reason=disabled:op1-needs-console-confirmation:3:" in p.stdout
    assert req == "96"


def test_tpm2_without_startup_fields_is_not_judged_disabled(tmp_path):
    p, req = _run(tmp_path, _tpm(tmp_path), CLEAN)
    assert p.returncode == 0 and "TPM-CLEAN" in p.stdout and req == "96"


# --- no EFI variable store: PPI steps fall back to the BIOS response --------

def _ro(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o555)
    return ro


def test_no_efi_store_ppi_clear_still_queues_and_is_judged_by_the_response(tmp_path):
    if os.geteuid() == 0:
        return
    ro = _ro(tmp_path)
    try:
        p, req = _run(tmp_path, _tpm(tmp_path / "a"), OWNED + ON2, efivars=str(ro))
        assert p.returncode == 10 and req == "5"
        p, _ = _run(tmp_path, _tpm(tmp_path / "b", response="5 0: Success"), OWNED + ON2, efivars=str(ro))
        assert p.returncode == 0 and "TPM-FAIL reason=still-owned-after-clear" in p.stdout
    finally:
        ro.chmod(0o755)


def test_no_efi_store_sce_is_a_fail(tmp_path):
    if os.geteuid() == 0:
        return
    ro = _ro(tmp_path)
    try:
        p, _ = _run(tmp_path, _tpm(tmp_path, ops=OPS12_PROMPTS), OWNED12, sce=SCE_OK, efivars=str(ro))
    finally:
        ro.chmod(0o755)
    assert p.returncode == 0 and "TPM-FAIL reason=efivar-marker-not-writable" in p.stdout
    assert not _ran(tmp_path, "sce")


# --- TPM 1.2: clear --------------------------------------------------------

def test_tpm12_owned_queues_op_22(tmp_path):
    p, req = _run(tmp_path, _tpm(tmp_path, ops=OPS12), OWNED12)
    assert p.returncode == 10 and "TPM-QUEUED op=22" in p.stdout and req == "22"


def test_tpm12_falls_back_to_op_5_when_22_prompts(tmp_path):
    p, req = _run(tmp_path, _tpm(tmp_path, ops=OPS12_22_PROMPTS), OWNED12)
    assert p.returncode == 10 and "TPM-QUEUED op=5" in p.stdout and req == "5"


def test_tpm12_op_22_confirmed(tmp_path):
    _mark(tmp_path, MARKER, "ppi22")
    p, req = _run(tmp_path, _tpm(tmp_path, ops=OPS12, response="22 0: Success"), CLEAN12)
    assert "TPM-CLEARED-AT-BOOT 22 0: Success" in p.stdout and "TPM-CLEAN" in p.stdout
    assert p.returncode == 0 and req == "96"


def test_tpm12_sce_clear_when_every_ppi_clear_prompts(tmp_path):
    p, req = _run(tmp_path, _tpm(tmp_path, ops=OPS12_PROMPTS), OWNED12, sce=SCE_OK)
    assert p.returncode == 10 and "TPM-QUEUED op=sce token=94" in p.stdout and req == "96"
    assert "op22-needs-console-confirmation" in p.stdout
    assert _marker_data(tmp_path, MARKER).startswith(b"\x07\x00\x00\x00sce ")


def test_tpm12_sce_clear_that_cannot_run_is_a_fail_without_a_marker(tmp_path):
    p, _ = _run(tmp_path, _tpm(tmp_path, ops=OPS12_PROMPTS), OWNED12,
                sce=(3, "SCE-SKIP reason=sce-unknown-bios:F06_3B99"))
    assert p.returncode == 0 and "TPM-FAIL reason=sce-unknown-bios:F06_3B99" in p.stdout
    assert not _has(tmp_path, MARKER)
    p, _ = _run(tmp_path, _tpm(tmp_path / "b", ops=OPS12_PROMPTS), OWNED12,
                sce=(1, "SCE-FAIL reason=sce-import-rc13"))
    assert "TPM-FAIL reason=sce-import-rc13" in p.stdout and not _has(tmp_path, MARKER)


def test_tpm12_sce_clear_confirmed_then_the_enable_follows(tmp_path):
    # The Leopard BIOS re-disables the TPM after its clear (et29b4 2026-10-28).
    _mark(tmp_path, MARKER, "sce")
    p, req = _run(tmp_path, _tpm(tmp_path, ops=OPS12_PROMPTS), OFF12, sce_enable=ENABLE_OK)
    assert "TPM-CLEARED-AT-BOOT sce" in p.stdout and not _has(tmp_path, MARKER)
    assert p.returncode == 10 and "TPM-QUEUED op=sce-enable" in p.stdout and _has(tmp_path, EMARKER)
    assert req == "96"                                       # never PPI op 6 on this BIOS


def test_tpm12_still_owned_after_sce_clear_fails_and_never_requeues(tmp_path):
    _mark(tmp_path, MARKER, "sce")
    p, _ = _run(tmp_path, _tpm(tmp_path, ops=OPS12_PROMPTS), OWNED12, sce=SCE_OK)
    assert p.returncode == 0 and "TPM-FAIL reason=still-owned-after-clear" in p.stdout
    assert not _ran(tmp_path, "sce") and _has(tmp_path, MARKER)


# --- TPM 1.2: enable -------------------------------------------------------

def test_tpm12_disabled_without_our_clear_is_enabled(tmp_path):
    # Post's own Leopard BIOS flash can reset setup to TPM State = Disabled.
    p, req = _run(tmp_path, _tpm(tmp_path, ops=OPS12_PROMPTS), OFF12, sce_enable=ENABLE_OK)
    assert p.returncode == 10 and "TPM-QUEUED op=sce-enable" in p.stdout and _has(tmp_path, EMARKER)
    assert req == "96"


def test_tpm12_owned_and_disabled_enables_before_the_clear(tmp_path):
    p, _ = _run(tmp_path, _tpm(tmp_path, ops=OPS12_PROMPTS), OWNED_OFF12, sce=SCE_OK, sce_enable=ENABLE_OK)
    assert p.returncode == 10 and "TPM-QUEUED op=sce-enable" in p.stdout and not _ran(tmp_path, "sce")


def test_tpm12_enable_confirmed_then_the_clear_follows(tmp_path):
    _mark(tmp_path, EMARKER, "sce")
    p, _ = _run(tmp_path, _tpm(tmp_path, ops=OPS12_PROMPTS), OWNED12, sce=SCE_OK)
    assert "TPM-ENABLED-AT-BOOT sce" in p.stdout and not _has(tmp_path, EMARKER)
    assert p.returncode == 10 and "TPM-QUEUED op=sce token=94" in p.stdout


def test_tpm12_enabled_after_the_sce_enable_is_clean(tmp_path):
    _mark(tmp_path, EMARKER, "sce")
    p, _ = _run(tmp_path, _tpm(tmp_path, ops=OPS12_PROMPTS), CLEAN12, sce_enable=ENABLE_OK)
    assert p.returncode == 0 and "TPM-ENABLED-AT-BOOT sce" in p.stdout and "TPM-CLEAN" in p.stdout
    assert not _has(tmp_path, EMARKER) and not _ran(tmp_path, "sce_enable")


def test_tpm12_still_disabled_after_the_sce_enable_fails_and_never_requeues(tmp_path):
    _mark(tmp_path, EMARKER, "sce")
    p, _ = _run(tmp_path, _tpm(tmp_path, ops=OPS12_PROMPTS), OFF12, sce_enable=ENABLE_OK)
    assert p.returncode == 0 and "TPM-FAIL reason=still-disabled-after-enable" in p.stdout
    assert not _ran(tmp_path, "sce_enable") and _has(tmp_path, EMARKER)


def test_tpm12_enable_that_sce_cannot_do_is_a_fail(tmp_path):
    p, _ = _run(tmp_path, _tpm(tmp_path, ops=OPS12_PROMPTS), OFF12,
                sce_enable=(3, "SCE-SKIP reason=sce-setup-already-enabled"))
    assert p.returncode == 0 and "TPM-FAIL reason=sce-setup-already-enabled" in p.stdout
    assert not _has(tmp_path, EMARKER)


def test_tpm12_state_never_takes_the_tpm2_enable(tmp_path):
    p, req = _run(tmp_path, _tpm(tmp_path, ops=OPS12 + "1 4: User not required\n"),
                  "owned=0 tpm=1.2 enabled=1 active=1 shEnable=0")
    assert p.returncode == 0 and "TPM-CLEAN" in p.stdout and req == "96"


# --- on: no TPM device -----------------------------------------------------

def test_no_tpm_device_turns_security_device_support_on(tmp_path):
    p, _ = _run(tmp_path, _tpm(tmp_path, tpm=False), "unused", sce_on=ON_OK)
    assert p.returncode == 10 and "TPM-QUEUED op=on token=91,92,95" in p.stdout
    assert _marker_data(tmp_path, OMARKER).startswith(b"\x07\x00\x00\x00sce ")


def test_no_tpm_device_with_setup_already_on_is_a_skip(tmp_path):
    p, _ = _run(tmp_path, _tpm(tmp_path, tpm=False), "unused",
                sce_on=(3, "SCE-SKIP reason=sce-setup-already-on"))
    assert p.returncode == 0 and "TPM-SKIP reason=no-tpm-device:sce-setup-already-on" in p.stdout
    assert not _has(tmp_path, OMARKER)


def test_no_tpm_device_on_a_board_sce_does_not_know_is_a_skip(tmp_path):
    p, _ = _run(tmp_path, _tpm(tmp_path, tpm=False), "unused",
                sce_on=(3, "SCE-SKIP reason=sce-unknown-bios:P26F"))
    assert p.returncode == 0 and "TPM-SKIP reason=no-tpm-device:sce-unknown-bios:P26F" in p.stdout


def test_still_no_tpm_after_turning_it_on_fails_and_never_requeues(tmp_path):
    _mark(tmp_path, OMARKER, "sce")
    p, _ = _run(tmp_path, _tpm(tmp_path, tpm=False), "unused", sce_on=ON_OK)
    assert p.returncode == 0 and "TPM-FAIL reason=still-no-tpm-after-security-device-enable" in p.stdout
    assert not _ran(tmp_path, "sce_on") and _has(tmp_path, OMARKER)


def test_tpm_on_confirmed_then_the_next_step_runs(tmp_path):
    _mark(tmp_path, OMARKER, "sce")
    p, _ = _run(tmp_path, _tpm(tmp_path, ops=OPS12_PROMPTS), OWNED_OFF12, sce_enable=ENABLE_OK)
    assert "TPM-ON-AT-BOOT sce" in p.stdout and not _has(tmp_path, OMARKER)
    assert p.returncode == 10 and "TPM-QUEUED op=sce-enable" in p.stdout


# --- whole Leopard journey: off + owned -> on, enabled, cleared, enabled ----

def test_tpm12_worst_case_takes_four_boots_and_ends_clean(tmp_path):
    efi = str(tmp_path / "efivars")
    steps = [  # (tpm present, state the BIOS leaves, expected op or CLEAN)
        (False, "unused", "op=on"),
        (True, OWNED_OFF12, "op=sce-enable"),
        (True, OWNED12, "op=sce "),
        (True, OFF12, "op=sce-enable"),
        (True, CLEAN12, "TPM-CLEAN"),
    ]
    for i, (present, state, want) in enumerate(steps):
        d = tmp_path / ("boot%d" % i)
        d.mkdir()
        p, _ = _run(d, _tpm(d, ops=OPS12_PROMPTS, tpm=present), state,
                    sce=SCE_OK, sce_enable=ENABLE_OK, sce_on=ON_OK, efivars=efi)
        assert want in p.stdout, (i, p.stdout)
        assert p.returncode == (0 if want == "TPM-CLEAN" else 10)
    assert os.listdir(efi) == []


# --- misc ------------------------------------------------------------------

def test_unreadable_tpm_is_a_fail(tmp_path):
    p, req = _run(tmp_path, _tpm(tmp_path), "owned=? error=TPM_rc=0x101")
    assert p.returncode == 0 and "TPM-FAIL reason=unreadable" in p.stdout and req == "96"


def test_progress_reaches_the_banner_through_ps_note(tmp_path):
    notes = str(tmp_path / "notes")
    p, _ = _run(tmp_path, _tpm(tmp_path), OWNED + ON2, note_log=notes)
    text = open(notes).read()
    assert "reading TPM state" in text and "queueing a BIOS clear (PPI op 5)" in text
