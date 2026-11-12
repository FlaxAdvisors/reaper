"""post.sh calls the status helper around every stage, and nothing else
changes. Static: post.sh cannot run here (it writes /tmp/post-<mac>, rsyncs
to root@bang and powers the box off); the live run is rollout step 4."""
import re
import subprocess


import pathlib as _pl
REAPER = _pl.Path(__file__).resolve().parents[1]

POST_SH = REAPER / "roles" / "apply_pxe_payloads" / "files" / "post" / "post.sh"
TEXT = POST_SH.read_text()


def _idx(needle, start=0):
    i = TEXT.find(needle, start)
    assert i >= 0, "missing: %r" % needle
    return i


def test_syntax():
    assert subprocess.run(["bash", "-n", str(POST_SH)]).returncode == 0


def test_no_errexit_or_nounset():
    assert not re.search(r"^\s*set\s+-[a-zA-Z]*[eu]", TEXT, re.M)


def test_sourced_and_initialised_before_postautomate_branch():
    assert _idx('. "$(dirname "$0")/post_status.sh"') < _idx('ps_init "$action"') \
        < _idx('if [ "$action" == "postautomate" ]')


def test_postautomate_shows_agent_and_finishes_waiting():
    block = TEXT[_idx('if [ "$action" == "postautomate" ]'):_idx("chronyd")]
    assert "ps_begin agent" in block and "ps_finish WAITING" in block
    assert block.index("ps_finish WAITING") < block.index("exit 0")


def test_stages_begin_in_run_order():
    order = ["ps_begin clock", "ps_begin ipmi", "ps_begin diskwipe", "ps_begin biosgate", "ps_begin nicfw",
             "ps_begin biosfw", "ps_begin memtest", "ps_begin inventory",
             "ps_begin binrefresh", "ps_begin tools", "ps_begin tpmclear", "ps_begin dump",
             "ps_begin poweroff"]
    pos = [_idx(n) for n in order]
    assert pos == sorted(pos), list(zip(order, pos))
    # ident is begun twice: in the memtest branch, and after tools in the
    # inventory branch -- the LAST one must sit between tools and dump.
    assert _idx("ps_begin tools") < TEXT.rindex("ps_begin ident") < _idx("ps_begin dump")
    assert TEXT.index("ps_begin ident") < _idx("ps_begin inventory")   # memtest one


def test_liveboot_and_cfgipmi_finish_waiting_before_exit():
    block = TEXT[_idx('if [ $action == "cfgipmi" ] || [ $action == "liveboot" ]'):]
    block = block[:re.search(r"\n\s*fi\s*\n", block).start()]   # not "fi" in ps_finish
    assert block.index("ps_finish WAITING") < block.index("exit 0")


def test_flash_steps_announce_themselves_first():
    assert _idx("ps_state POWER-OFF", _idx("ps_begin biosgate")) < _idx("./bios_gate_report.sh")
    assert _idx("ps_state REBOOTING", _idx("ps_begin nicfw")) < _idx("./update_mellanox.sh")
    assert _idx("ps_state REBOOTING", _idx("ps_begin biosfw")) < _idx("./update_quanta_leopard.sh $biosver")


def test_skips_are_recorded_for_platforms_without_them():
    for s in ('ps_skip biosgate', 'ps_skip nicfw', 'ps_skip biosfw', 'ps_skip diskwipe', 'ps_skip tpmclear'):
        assert s in TEXT


def test_identity_and_logdir_feed_the_status():
    assert _idx('ps_set mac "$mac"') > _idx("mac=$(cat /proc/cmdline")
    assert _idx('ps_set logdir "$logdir"') > _idx("mkdir -p $logdir")
    assert 'ps_set ipmigood "$ipmigood"' in TEXT


def test_dump_result_is_recorded_and_poweroff_finishes():
    assert re.search(r"if rsync -SHAXav \$macdir \$dstdir; then\s+(dumpok=1\s+)?ps_done", TEXT)
    assert "ps_fail" in TEXT[_idx("ps_begin dump"):]
    tail = TEXT[_idx("ps_begin poweroff"):]
    assert tail.index("ps_finish POWER-OFF") < tail.index("ipmitool chassis power off")


def test_original_commands_are_all_still_there():
    for cmd in ("chronyd -q", "hwclock --systohc", "./setipmiuser.sh", "./bios_gate_report.sh || true",
                "./update_mellanox.sh", "./update_quanta_leopard.sh $biosver",
                "hwinfo --arch", "ipmitool sel elist", "/opt/flax/bin/dimmerr",
                "./collect_mellanox.sh $logdir", "smartctl --all", "rsync -SHAXav $macdir $dstdir",
                "ipmitool chassis power off", "shutdown -h now"):
        assert cmd in TEXT, cmd


def test_collected_is_not_a_pass_and_says_check_the_gui():
    # Operator, 2026-09-25: collecting inventory is not a verdict. After the
    # dump the last word on SOL must send the operator to the GUI, and must
    # not read as "done, pull it".
    tail = TEXT[_idx("ps_begin dump"):]
    assert "INVENTORY COLLECTED" in tail and "CHECK THE GUI" in tail
    assert "NOT delivered" in tail                      # the dump-failed wording
    assert "$action done -- powering off." not in TEXT


def test_the_final_frame_gets_time_to_reach_sol():
    # et24b3, 2026-09-25: ps_finish then `ipmitool chassis power off` within
    # milliseconds -- the last frame SOL ever showed was "RUNNING dump".
    # Flushed, not slept (operator, 2026-09-25): ps_flush waits for the
    # UART to finish sending the repaint, then syncs.
    tail = TEXT[_idx("ps_finish POWER-OFF"):]
    before_cut = tail[:tail.index("ipmitool chassis power off")]
    assert re.search(r"^\s*ps_flush\b", before_cut, re.M), before_cut
    assert not re.search(r"^\s*sleep [0-9]", before_cut, re.M)


def test_diskwipe_is_inventory_only_gated_on_flaxwipe_and_before_any_reboot():
    # 2026-11-10: the wipe moved ahead of the BIOS gate / NIC flash / Leopard
    # flash -- any of them can reboot the node, and a reboot before the wipe
    # boots the old OS off a disk-first boot order.
    block = TEXT[_idx("ps_begin diskwipe") - 800:_idx("ps_begin biosgate")]
    assert 'if [ "$action" == "inventory" ]; then' in block
    assert 'if [ "$flaxwipe" == "1" ]; then' in block
    assert "flaxwipe=$(sed -n" in block and "/proc/cmdline" in block
    assert "./wipe_disks.sh > $logdir/diskwipe.txt" in block
    # staylive (liveboot) and cfgipmi exit before the wipe is ever reached
    assert _idx('if [ $action == "cfgipmi" ] || [ $action == "liveboot" ]') < _idx("ps_begin diskwipe")
    assert _idx('mkdir -p $logdir') < _idx("ps_begin diskwipe") < _idx("./bios_gate_report.sh")


def test_tpmclear_shares_the_wipe_gate_and_exports_the_banner_note():
    assert _idx("export -f _ps ps_note") < _idx("./wipe_disks.sh") < _idx("./tpm_clear.sh")
    gate = _idx('if [ "$action" == "inventory" ] && [ "$flaxwipe" == "1" ]; then')
    block = TEXT[gate:_idx("ps_begin ident", gate)]
    assert "./tpm_clear.sh" in block
    assert "tpmq=1" in block and "-eq 10" in block
    assert _idx("ps_begin tools") < gate


def test_queued_tpm_clear_reboots_with_a_reason_instead_of_powering_off():
    i = TEXT.rindex('if [ "$tpmq" = 1 ]; then')   # the reboot branch, not the ident guard
    block = TEXT[i:_idx("ps_begin poweroff", i)]
    assert _idx("ps_begin dump") < i < _idx("ps_begin poweroff")   # dump first, never skipped
    assert "ps_finish REBOOTING" in block and "TPM" in block and "Not a crash" in block
    assert block.index("ps_flush") < block.index("systemctl reboot") < block.index("exit 0")
    assert "ipmitool chassis power off" not in block


def test_queued_tpm_clear_never_lights_the_ident_led():
    # Operator, 2026-10-27: chassis identify is THE "pull it" cue. A run that
    # reboots for the TPM clear is not finished, so it must not identify; the
    # BMC keeps a 180 s identify blinking straight through the reboot.
    i = TEXT.rindex("ps_begin ident")
    block = TEXT[i - 200:_idx("fi # end of if memtest else clause", i)]
    assert re.search(r'if \[ "\$tpmq" = 1 \]; then\s+ps_skip ident', block), block
    gated = block[block.index('if [ "$tpmq" = 1 ]'):]
    assert gated.index("ps_skip ident") < gated.index("else") < gated.index("ipmitool chassis identify 180")
