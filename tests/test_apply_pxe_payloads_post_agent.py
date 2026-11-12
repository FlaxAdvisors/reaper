import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[1]

POST = (REPO / "roles/apply_pxe_payloads/files/post")
ROLE_TASKS = (REPO / "roles/apply_pxe_payloads/tasks/main.yml")
DEFAULTS = (REPO / "roles/apply_pxe_payloads/defaults/main.yml")
INV = (REPO / "inventories")


def _drop_items():
    """The --exclude patterns of the build-dir rsync (excluded = never shipped, and
    deleted from the build dir if present)."""
    import re
    tasks = ROLE_TASKS.read_text()
    after = tasks.split("Sync post text source into the build dir")[1].split("- name:")[0]
    assert "'--delete-excluded'" in after, "excluded files are no longer deleted from the build dir"
    # the list literal that is mapped to --exclude= (anchored on the mapping, not on
    # whatever rsync option happens to precede it)
    m = re.search(r"\[([^\[\]]*)\]\s*\|\s*map\('regex_replace',\s*'\^',\s*'--exclude='\)", after)
    assert m, "no --exclude= list in the post build-dir sync"
    return re.findall(r"'([^']+)'", m.group(1))


def _dropped(name):
    import fnmatch
    return any(fnmatch.fnmatch(name, p) for p in _drop_items())


def test_post_sh_postautomate_branch_short_circuits_before_ntp():
    sh = (POST / "post.sh").read_text()
    assert "postautomate" in sh and "qual_agent.py" in sh
    # must short-circuit before the NTP/IPMI setup (the agent does its own in-band IPMI)
    assert sh.index("postautomate") < sh.index("chronyd")
    r = subprocess.run(["bash", "-n", str(POST / "post.sh")],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert r.returncode == 0, r.stdout.decode()


def test_post_sh_launches_agent_detached_from_banghook_cgroup():
    # banghook.service is Type=oneshot (no RemainAfterExit): a bare `&` child is
    # reaped when post.sh exits, so the agent must run in its own systemd unit.
    sh = (POST / "post.sh").read_text()
    assert "systemd-run" in sh


def test_agent_modules_present_in_post_dir():
    for m in ("qual_stages.py", "qual_battery.py", "qual_server.py", "qual_agent.py"):
        assert (POST / m).is_file(), m


def test_role_drops_qual_test_files_but_not_shipping_modules():
    for t in ("test_qual_stages.py", "test_qual_battery.py", "test_qual_server.py",
              "test_qual_agent.py", "test_qual_contract.py", ".pytest_cache", "__pycache__"):
        assert _dropped(t), "%s must be dropped so it doesn't ship" % t
    for m in ("qual_stages.py", "qual_battery.py", "qual_server.py", "qual_agent.py"):
        assert not _dropped(m), "%s must ship (must NOT be in the drop list)" % m


def test_post_sh_passes_per_site_env_file_to_agent_unit():
    # the agent unit loads its per-site runtime config (FLAX_IPERF_SERVER); the
    # leading '-' makes the EnvironmentFile optional so a missing file is non-fatal
    sh = (POST / "post.sh").read_text()
    assert "EnvironmentFile=-" in sh
    assert "flax-qual.env" in sh


def test_role_renders_flax_qual_env_and_drops_the_template():
    tasks = ROLE_TASKS.read_text()
    assert _dropped("flax-qual.env.j2")                           # the .j2 must NOT ship
    assert "{{ post_build_dir }}/flax-qual.env" in tasks          # a render task ships flax-qual.env


def test_flax_qual_env_template_sets_iperf_server():
    j2 = (POST / "flax-qual.env.j2").read_text()
    assert "FLAX_IPERF_SERVER={{ post_iperf_server }}" in j2


def test_iperf_server_is_each_sites_backup_bang():
    # derived once for every site: the site's standby (never another site's bang)
    placement = (REPO / "playbooks/group_vars/all/placement.yml").read_text()
    assert 'post_iperf_server: "{{ site_standby_bangs | first | default(site_primary_bang) }}"' in placement
    assert "post_iperf_server:" not in DEFAULTS.read_text()
    for site in ("eindhoven", "braintree", "lavergne"):
        assert "post_iperf_server:" not in (INV / site / "group_vars/all/vars.yml").read_text(), site


def test_post_sh_honors_action_arg_override():
    # the engine advances a staylive-booted node with `post.sh postautomate` (no reboot)
    sh = (POST / "post.sh").read_text()
    assert '[ -n "$1" ] && action="$1"' in sh


def test_post_sh_collects_dimmerr_into_the_node_dump():
    """DIMM health (EDAC CE/UE) must land in the node dump, not just on the
    operator's terminal.

    Before this, dimmerr only ran ad hoc at post stage 61 via `ansible -m
    script`, printing to the terminal -- nothing durable reached
    /export/nodes/post-<mac>/latest/, so the triage UI could not show DIMM
    health. dimmsum (inventory) was collected; dimmerr (health) was not.
    """
    sh = (POST / "post.sh").read_text()
    assert "/opt/flax/bin/dimmerr" in sh, "dimmerr is never invoked"
    assert "$logdir/dimmerr.txt" in sh, "dimmerr output is not captured"


def test_post_sh_dimmerr_writes_stdout_only_to_the_file():
    """Match the convention of the surrounding bin calls: `2>&1 > file` sends
    stderr to the console and stdout to the file. That ordering is load-bearing
    here -- dimmerr shells `sudo -n dmidecode`, and folding its warnings into
    the data file would corrupt the parse downstream."""
    sh = (POST / "post.sh").read_text()
    line = [ln for ln in sh.splitlines()
            if "/opt/flax/bin/dimmerr" in ln and not ln.strip().startswith("#")]
    assert len(line) == 1, f"expected exactly one dimmerr invocation, got {line}"
    assert "2>&1 > $logdir/dimmerr.txt" in line[0], line[0]


def test_post_sh_collects_both_dimm_inventory_and_dimm_health():
    """dimmsum and dimmerr answer different questions and both belong in the
    dump: what the DIMMs ARE, and whether they are erroring."""
    sh = (POST / "post.sh").read_text()
    for tool in ("dimmsum", "dimmerr"):
        assert f"/opt/flax/bin/{tool}" in sh, f"{tool} not collected"


def test_dimmerr_is_bundled_into_post_tgz():
    """Collecting it is useless if the binary never reaches the live ISO."""
    assert "dimmerr" in DEFAULTS.read_text()


def test_post_sh_refreshes_flax_bin_before_invoking_it():
    """post.sh calls /opt/flax/bin/<tool> by absolute path, but that directory
    on a live-booted DUT is populated by a SEPARATE ansible stage (ghost
    flax_user.yml, donum 54). When that stage stopped running -- a tab-indented
    list item made the playbook unparseable -- dimmerr never reached a node and
    dimmerr.txt came back 0 bytes on every fresh inventory.

    The refresh must come BEFORE the first absolute-path invocation, or it
    fixes nothing.
    """
    sh = (POST / "post.sh").read_text()
    refresh = sh.find("rsync -a --timeout=20")
    first_call = sh.find("/opt/flax/bin/dimmsum")
    assert refresh != -1, "no /opt/flax/bin refresh in post.sh"
    assert first_call != -1
    assert refresh < first_call, "the refresh runs after the tools it is meant to supply"


def test_bin_refresh_reuses_the_existing_bang_target():
    """$dst is the same root@bang the dump rsync at the end of the script
    already uses -- the refresh must not introduce a second credential or a
    hardcoded host."""
    sh = (POST / "post.sh").read_text()
    block = sh.split("rsync -a --timeout=20")[1].split("fi")[0]
    assert "${dst}:/opt/flax/bin/" in block, block


def test_bin_refresh_is_non_destructive():
    """-a without --delete only adds and updates. A --delete here would wipe
    tools the node legitimately has that the bang does not."""
    sh = (POST / "post.sh").read_text()
    block = sh.split("rsync -a --timeout=20")[1].split("fi")[0]
    assert "--delete" not in block


def test_bin_refresh_falls_back_to_the_bundled_copies():
    """A DUT that cannot reach the bang must still inventory. post.tgz bundles
    exactly the seven tools post.sh invokes, extracted alongside it; the fallback
    installs them through the same install_bundled_bins the postautomate launch
    uses (its list and behaviour are tested below)."""
    sh = (POST / "post.sh").read_text()
    block = sh.split("rsync -a --timeout=20")[1].split("\nfi")[0]
    assert "install_bundled_bins" in block, block
    for tool in ("macinv", "dimmerr", "dimmsum", "lsnet", "alldisks", "bootorder", "spichip"):
        assert tool in _install_fn(sh), f"{tool} missing from the bundle list"


def test_post_sh_records_the_bios_chip_with_spichip():
    """The BIOS flash chip part (MX25L25635F vs the 4-byte-only MX25L25735F a
    Quanta cannot boot from) lands in the node dump as spichip.txt. post.sh
    calls the standalone util, after the bin refresh that delivers it, rather
    than decoding the SFDP table itself."""
    sh = (POST / "post.sh").read_text()
    line = [ln for ln in sh.splitlines()
            if "/opt/flax/bin/spichip" in ln and not ln.strip().startswith("#")]
    assert len(line) == 1, f"expected exactly one spichip invocation, got {line}"
    assert "-v" in line[0] and "2>&1 > $logdir/spichip.txt" in line[0], line[0]
    assert sh.index("rsync -a --timeout=20") < sh.index("/opt/flax/bin/spichip -v")
    assert "spi-nor.txt" not in sh and "sfdp=$(od" not in sh
    assert "- spichip" in DEFAULTS.read_text()


def test_post_sh_is_valid_bash():
    r = subprocess.run(["bash", "-n", str(POST / "post.sh")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# --- bundled tools -> /opt/flax/bin (2026-09-28) ----------------------------
# The live ISO bakes its own /opt/flax/bin (Feb 2025: macinv printed every FRU as
# "Board Mfg:", dimmsum/bootorder older, no dimmerr). The engine's `post.sh
# postautomate` launch never ran the bang rsync, so those copies were what ran.
# post.tgz bundles the bang's current copies; unpacking the bundle installs them.

def _install_fn(sh):
    """The bundle_bins list + install_bundled_bins() definition, verbatim."""
    start = sh.index("bundle_bins=")
    end = sh.index("\n}\n", sh.index("function install_bundled_bins")) + 3
    return sh[start:end]


def test_bundled_bins_match_the_post_tgz_build_list():
    """Adding a tool to post_build_binaries must also install it on the node."""
    import re, yaml
    sh = (POST / "post.sh").read_text()
    listed = re.search(r'^bundle_bins="([^"]*)"', sh, re.M).group(1).split()
    built = yaml.safe_load(DEFAULTS.read_text())["post_build_binaries"]
    assert listed == built


def test_install_bundled_bins_replaces_stale_and_adds_missing(tmp_path):
    """Real bash: a stale ISO copy is replaced, a missing tool is added, an
    identical one is left alone (mtime unchanged), a tool outside the list is
    untouched."""
    import os
    bundle, flaxbin = tmp_path / "hook", tmp_path / "bin"
    bundle.mkdir(); flaxbin.mkdir()
    sh = (POST / "post.sh").read_text()
    names = __import__("re").search(r'^bundle_bins="([^"]*)"', sh, __import__("re").M).group(1).split()
    for b in names:
        (bundle / b).write_text("#!/bin/sh\necho new-%s\n" % b)
    (flaxbin / "macinv").write_text("#!/bin/sh\necho iso-macinv\n")        # stale
    (flaxbin / "lsnet").write_text("#!/bin/sh\necho new-lsnet\n")          # identical
    (flaxbin / "lsnet").chmod(0o755)                                        # as the ISO ships it
    os.utime(flaxbin / "lsnet", (1, 1))
    (flaxbin / "macformat").write_text("iso-macformat\n")                  # not bundled
    script = _install_fn(sh) + "\ninstall_bundled_bins\n"
    r = subprocess.run(["bash", "-c", script, str(bundle / "post.sh")],
                       env=dict(os.environ, FLAX_BIN=str(flaxbin)),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    for b in names:
        assert (flaxbin / b).read_text() == "#!/bin/sh\necho new-%s\n" % b, b
        assert os.access(flaxbin / b, os.X_OK), b
    assert os.stat(flaxbin / "lsnet").st_mtime == 1                        # not rewritten
    assert (flaxbin / "macformat").read_text() == "iso-macformat\n"


def test_postautomate_installs_the_bundle_before_starting_the_agent():
    sh = (POST / "post.sh").read_text()
    branch = sh.split('if [ "$action" == "postautomate" ]; then')[1].split("\nfi\n")[0]
    assert "install_bundled_bins" in branch
    assert branch.index("install_bundled_bins") < branch.index("systemd-run")
    # defined before the branch that calls it (bash resolves at call time, but
    # the branch exits the script, so a later definition is never read)
    assert sh.index("function install_bundled_bins") < sh.index('if [ "$action" == "postautomate" ]')
