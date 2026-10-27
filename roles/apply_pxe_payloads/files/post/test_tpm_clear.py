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


def _run(ppi, state, note_log=None):
    env = dict(os.environ, TPM_PPI_DIR=ppi, TPM_STATE_CMD="echo " + state)
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


def test_tpm12_skips_when_no_clear_op_is_unattended(tmp_path):
    p, req = _run(_ppi(str(tmp_path), ops=OPS12_NONE), OWNED12)
    assert p.returncode == 0 and "reason=op22-needs-console-confirmation" in p.stdout and req == "96"


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
