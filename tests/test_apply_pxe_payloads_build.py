import pathlib

import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]

TASKS = (REPO / "roles/apply_pxe_payloads/tasks/main.yml").read_text()
DEFAULTS = (REPO / "roles/apply_pxe_payloads/defaults/main.yml").read_text()


def test_yaml_is_valid():
    yaml.safe_load(TASKS)
    yaml.safe_load(DEFAULTS)


def test_build_sources_creds_from_vaulted_json_no_log():
    # setipmiuser password comes from the already-vaulted credentials-bmc.json
    assert "credentials-bmc.json" in TASKS
    assert "post_ipmi_root_password" in TASKS and "post_ipmi_admin_password" in TASKS
    assert "post_ipmi_admin_user" in TASKS
    # the cred-loading + template tasks are no_log so secrets never reach ansible output
    assert TASKS.count("no_log: true") >= 3


def test_build_templates_setipmiuser_and_pulls_binaries():
    assert "setipmiuser.sh.j2" in TASKS and "ansible.builtin.template:" in TASKS
    assert "regex_replace', '^', '/opt/flax/bin/')" in TASKS
    assert "post_build_binaries" in TASKS


def test_build_tars_to_single_post_tgz():
    assert "tar -C" in TASKS and "post.tgz" in TASKS


def test_defaults_declare_build_dir_and_binaries():
    assert "post_build_dir:" in DEFAULTS
    # ghost inventory bins are bundled; platform-specific AMI tools (SCELNX_64/afulnx_64)
    # are NOT bundled (fetched per-platform at runtime).
    for b in ("macinv", "dimmerr", "dimmsum", "lsnet", "alldisks", "bootorder", "spichip"):
        assert b in DEFAULTS
    assert "SCELNX_64" not in DEFAULTS.split("post_build_binaries:")[1]


def test_post_tgz_symlinked_not_copied_per_vid():
    # the copy loop no longer stages post.tgz per-vid...
    assert "'boot.ipxe.cfg', 'menu.ipxe', 'post.tgz'" not in TASKS
    assert "files/v/boot.ipxe.cfg" in TASKS and "files/v/menu.ipxe" in TASKS   # staged per vid, one rsync
    # ...it's a relative symlink to the single built tarball (like ipxe)
    assert "../post.tgz" in TASKS


def test_committed_blob_gone_from_working_tree():
    assert not (REPO / "roles/apply_pxe_payloads/files/v/post.tgz").exists()


def _task(name_part):
    tasks = yaml.safe_load(TASKS)
    return next(t for t in tasks if name_part in t.get("name", ""))


def test_build_ships_the_station_serial_helpers():
    t = _task("SOL console helpers")
    assert "scripts/unlock-with-ubuntu/payload/" in t["ansible.builtin.copy"]["src"]
    assert sorted(t["loop"]) == ["serial_console_fixup.sh", "serial_watchdog.sh"]
    assert "post_tgz" in t["tags"]


def test_build_ships_setserial_from_the_bang():
    t = _task("and setserial")
    script = t["ansible.builtin.shell"]
    assert "/usr/bin/setserial" in script and "install -m 0755" in script
    assert "post_tgz" in t["tags"]


def test_build_writes_a_BUILD_stamp():
    t = _task("Stamp the build")
    c = t["ansible.builtin.copy"]
    assert c["dest"].endswith("/BUILD")
    # the last commit touching the post inputs, no timestamp (a timestamp made every build new)
    assert "log -1 --format=%h" in c["content"] and "-dirty" in c["content"]
    assert "date" not in c["content"]


def test_extras_land_before_the_tarball_is_made():
    names = [t.get("name", "") for t in yaml.safe_load(TASKS)]
    tar = next(i for i, n in enumerate(names) if n.startswith("Build ") and "post.tgz" in n)
    for part in ("SOL console helpers", "setserial", "Stamp the build"):
        assert next(i for i, n in enumerate(names) if part in n) < tar, part


def test_status_files_are_executable_in_git():
    import subprocess
    REAPER = pathlib.Path(__file__).resolve().parents[1]
    out = subprocess.run(["git", "-C", str(REAPER), "ls-files", "-s",
                          "roles/apply_pxe_payloads/files/post/post_status.py",
                          "roles/apply_pxe_payloads/files/post/poststate"],
                         capture_output=True, text=True).stdout
    assert out.count("100755") == 2, out
