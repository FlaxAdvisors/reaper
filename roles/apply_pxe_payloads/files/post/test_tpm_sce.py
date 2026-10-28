"""tpm_sce.py against a fake SCELNX_64 that dumps/imports a setup script file."""
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tpm_sce  # noqa: E402

SCRIPT = os.path.join(HERE, "tpm_sce.py")

HEADER = (b"// Script File Name : /tmp/nvscript.txt\n"
          b"// Created on 10/27/26 at 23:04:19\t\n"
          b"// Copyright (c)2014 American Megatrends, Inc.\n"
          b"// AMISCE Utility. Ver 5.00.1059\n"
          b"\r\n"
          b"HIICrc32= 492F03E4\r\n"
          b"\n")


def _q(name, token, offset, default, opts):
    lines = ["Setup Question\t= %s" % name,
             "Token\t=%s\t// Do NOT change this line" % token,
             "Offset\t=%s" % offset, "Width\t=01",
             "BIOS Default\t=%s" % default]
    for i, o in enumerate(opts):
        lines.append(("Options\t=%s\t// Move \"*\" to the desired Option" if i == 0 else "         %s") % o)
    return ("\n".join(lines) + "\n\n").encode()


# Token 94 is ALSO "Port 4" on this BIOS (et31b4 F06_3B17): selection must be by name.
DUMP = (HEADER
        + _q("  Port 4", "94", "136", "[01]Enabled", ["[00]Disabled", "*[01]Enabled"])
        + _q("  TPM State", "93", "01", "[01]Enabled", ["*[00]Disabled", "[01]Enabled"])
        + _q("  TCM State", "96", "01", "[01]Enabled", ["*[00]Disabled", "[01]Enabled"])
        + _q("Pending operation", "94", "02", "[00]None", ["*[00]None", "[05]TPM Clear"])
        + _q("Pending operation", "97", "02", "[00]None", ["*[00]None", "[05]TPM Clear"])
        + _q("  Pending operation", "A5", "02", "[00]None", ["*[00]None", "[01]TPM Clear"]))


def test_finds_the_tpm12_clear_question_by_name_not_token():
    q = tpm_sce.find_clear_question(DUMP)
    assert b"Setup Question\t= Pending operation" in q and b"Offset\t=02" in q
    assert b"Port 4" not in q and b"Token\t=94" in q


def test_import_script_keeps_the_dump_header_bytes_and_moves_the_star():
    s = tpm_sce.build_clear_script(DUMP)
    assert s.startswith(HEADER)                       # CRs and all: SCE rejects anything else
    body = s[len(HEADER):]
    assert body.count(b"Setup Question") == 1
    assert b"Options\t=[00]None\t// Move" in body and b"         *[05]TPM Clear" in body


def test_selected_value():
    assert tpm_sce.selected(tpm_sce.find_clear_question(DUMP)) == "00"
    assert tpm_sce.selected(tpm_sce.find_clear_question(tpm_sce.build_clear_script(DUMP))) == "05"


def test_unknown_bios_build_is_refused():
    with pytest.raises(tpm_sce.Skip, match="unknown-hii:DEADBEEF"):
        tpm_sce.build_clear_script(DUMP.replace(b"492F03E4", b"DEADBEEF"))


def test_missing_question_is_refused():
    with pytest.raises(tpm_sce.Skip, match="no-clear-question"):
        tpm_sce.build_clear_script(HEADER + _q("  TPM State", "93", "01", "[01]Enabled",
                                               ["[00]Disabled", "*[01]Enabled"]))


# --- the CLI against a fake SCELNX_64 -------------------------------------
# The fake keeps "NVRAM" as a file: /o copies it out, /i applies the one
# question's selection to it (or refuses, or ignores it, per FAKE_SCE_MODE).
FAKE = r'''#!/usr/bin/env python3
import os, re, shutil, sys
nv = os.environ["FAKE_SCE_NV"]
mode = os.environ.get("FAKE_SCE_MODE", "ok")
args = sys.argv[1:]
open(os.environ["FAKE_SCE_LOG"], "a").write(" ".join(args) + "\n")
if args[0] == "/o":
    shutil.copy(nv, args[2]); print("Script file exported successfully."); sys.exit(0)
if mode == "reject":
    print("- ERROR: Script file error."); sys.exit(13)
if mode == "ignore":
    sys.exit(0)
d = open(nv, "rb").read()
new = open(args[2], "rb").read()
q = new[new.index(b"Setup Question"):]
name_line = q.split(b"\n", 1)[0] + b"\n"
want = re.search(rb"\*\[(..)\]", q.split(b"Options", 1)[1]).group(1)
i = d.index(name_line)
j = d.index(b"\n\n", i)
head, o2 = d[i:j].split(b"Options", 1)
blk = head + b"Options" + o2.replace(b"*[", b"[").replace(b"[" + want + b"]", b"*[" + want + b"]")
open(nv, "wb").write(d[:i] + blk + d[j:]); print("Script file imported successfully.")
'''


def _cli(tmp_path, mode="ok", dump=DUMP, cmd="clear"):
    nv = tmp_path / "nv.txt"
    nv.write_bytes(dump)
    fake = tmp_path / "SCELNX_64"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    log = tmp_path / "log"
    env = dict(os.environ, TPM_SCE_BIN=str(fake), FAKE_SCE_NV=str(nv), FAKE_SCE_MODE=mode,
               FAKE_SCE_LOG=str(log), TMPDIR=str(tmp_path))
    p = subprocess.run([sys.executable, SCRIPT, cmd], env=env, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, universal_newlines=True, timeout=60)
    return p, nv.read_bytes(), log.read_text() if log.exists() else ""


def test_cli_queues_the_clear_and_verifies_it(tmp_path):
    p, nv, log = _cli(tmp_path)
    assert p.returncode == 0 and "SCE-QUEUED token=94 hii=492F03E4" in p.stdout
    assert tpm_sce.selected(tpm_sce.find_clear_question(nv)) == "05"
    assert [l.split()[0] for l in log.splitlines()] == ["/o", "/i", "/o"]


def test_cli_import_rejected_is_a_failure(tmp_path):
    p, nv, _ = _cli(tmp_path, mode="reject")
    assert p.returncode == 1 and "reason=sce-import-rc13" in p.stdout
    assert tpm_sce.selected(tpm_sce.find_clear_question(nv)) == "00"


def test_cli_import_that_did_not_stick_is_a_failure(tmp_path):
    p, _, _ = _cli(tmp_path, mode="ignore")
    assert p.returncode == 1 and "reason=sce-clear-did-not-stick" in p.stdout


def test_cli_unknown_bios_is_a_skip_and_writes_nothing(tmp_path):
    p, _, log = _cli(tmp_path, dump=DUMP.replace(b"492F03E4", b"DEADBEEF"))
    assert p.returncode == 3 and "reason=sce-unknown-hii:DEADBEEF" in p.stdout
    assert "/i" not in log


# --- enable: TPM State back to Enabled (et29b4 2026-10-28) -------------------
# After a 1.2 clear the BIOS syncs its setup to "TPM State = Disabled" and
# re-applies it every POST, overriding PPI op 6 ("6 0: Success", still
# disabled, two blades of two). Writing TPM State = Enabled through SCE and
# rebooting brought et29b4 back to enabled=1 active=1.

def test_enable_question_is_tpm_state_not_tcm_state():
    q = tpm_sce.find_enable_question(DUMP)
    assert q.startswith(b"Setup Question\t=   TPM State") and b"Token\t=93" in q


def test_enable_script_moves_the_star_in_options_only():
    # The BIOS Default line also reads [01]Enabled: touching it is SCE rc 13
    # "Unexpected BIOS Default" (et29b4, first try, 2026-10-28).
    s = tpm_sce.build_enable_script(DUMP)
    assert s.startswith(HEADER)
    body = s[len(HEADER):]
    assert b"BIOS Default\t=[01]Enabled\n" in body
    assert b"Options\t=[00]Disabled" in body and b"         *[01]Enabled" in body
    assert body.count(b"*[") == 1


def test_enable_when_setup_already_says_enabled_is_a_skip():
    d = DUMP.replace(b"Options\t=*[00]Disabled\t// Move \"*\" to the desired Option\n         [01]Enabled",
                     b"Options\t=[00]Disabled\t// Move \"*\" to the desired Option\n         *[01]Enabled", 1)
    assert tpm_sce.selected(tpm_sce.find_enable_question(d)) == "01"
    with pytest.raises(tpm_sce.Skip, match="setup-already-enabled"):
        tpm_sce.build_enable_script(d)


def test_clear_script_never_touches_bios_default():
    d = DUMP.replace(b"BIOS Default\t=[00]None", b"BIOS Default\t=[05]TPM Clear", 1)
    s = tpm_sce.build_clear_script(d)
    assert b"BIOS Default\t=[05]TPM Clear\n" in s and b"         *[05]TPM Clear" in s


def test_cli_enable_queues_and_verifies(tmp_path):
    p, nv, log = _cli(tmp_path, cmd="enable")
    assert p.returncode == 0 and "SCE-QUEUED token=93 hii=492F03E4" in p.stdout
    assert tpm_sce.selected(tpm_sce.find_enable_question(nv)) == "01"
    assert tpm_sce.selected(tpm_sce.find_clear_question(nv)) == "00"      # nothing else written


def test_cli_enable_unknown_bios_is_a_skip(tmp_path):
    p, _, log = _cli(tmp_path, cmd="enable", dump=DUMP.replace(b"492F03E4", b"DEADBEEF"))
    assert p.returncode == 3 and "reason=sce-unknown-hii:DEADBEEF" in p.stdout and "/i" not in log
