"""apply_install_media: every bang holds (sha256-verified) and loop-mounts the ISOs the
managed installs boot (operator 2026-11-10). Spec: 2026-11-10-install-files-into-reaper-design.md."""
import hashlib
import importlib.util
import os
import re

import pytest
import yaml

_REAPER = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
ROLE = os.path.join(_REAPER, "roles", "apply_install_media")
MANIFEST = os.path.join(_REAPER, "files", "install-media.yml")

_spec = importlib.util.spec_from_file_location("fim", os.path.join(ROLE, "files", "flax_install_media.py"))
fim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fim)

GOOD = b"good iso bytes" * 100
BAD = b"stale or re-spun" * 100


def _entry(path="a/b.iso", data=GOOD, url=None):
    return {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "url": url, "mount": None}


def _boom(*a, **k):
    raise AssertionError("must not fetch")


def test_manifest_shape():
    m = yaml.safe_load(open(MANIFEST))["isos"]
    assert m
    for e in m:
        assert set(e) == {"path", "size", "sha256", "url", "mount"}
        assert re.fullmatch(r"[0-9a-f]{64}", e["sha256"]) and isinstance(e["size"], int) and e["size"] > 0
        for p in (e["path"], e["mount"] or "x"):
            assert not p.startswith("/") and ".." not in p.split("/")


def test_manifest_covers_managed_installs():
    m = yaml.safe_load(open(MANIFEST))["isos"]
    mounts = {e["mount"] for e in m if e["mount"]}
    need = {"canonical/ubuntu/18.04/iso", "canonical/ubuntu/22.04/ga", "canonical/ubuntu/24.04/ga",
            "suse/leap/15.6/ga", "redhat/centos/7/ga",
            # were ghost mountisos's last entries (operator 2026-11-10: Leap 15.1 + Harvester 1.2-1.4 dropped)
            "canonical/ubuntu/20.04/iso", "onie/wedge100/ga", "onie/wedge100s/ga"}
    assert need <= mounts
    ipxe = open(os.path.join(_REAPER, "roles/apply_pxe_payloads/templates/v/boot.ipxe.j2")).read()
    for t in need:
        assert f"set install ${{install-url}}/{t}" in ipxe


def test_verified_iso_is_left_alone(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a/b.iso").write_bytes(GOOD)
    st = fim.ensure(_entry(), root=str(tmp_path), peers=["p1"], fetch_url=_boom, fetch_peer=_boom,
                    free_bytes=lambda p: 10**12)
    assert st == "present"
    assert fim.ensure(_entry(), root=str(tmp_path), peers=["p1"], fetch_url=_boom, fetch_peer=_boom,
                      free_bytes=lambda p: 10**12) == "present"     # second run: from the cache


def test_wrong_checksum_is_replaced_from_a_peer(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a/b.iso").write_bytes(BAD)
    tried = []

    def peer(host, path, part):
        tried.append(host)
        open(part, "wb").write(GOOD if host == "p2" else BAD)
        return host == "p2" or None

    st = fim.ensure(_entry(), root=str(tmp_path), peers=["p1", "p2"], fetch_url=_boom, fetch_peer=peer,
                    free_bytes=lambda p: 10**12)
    assert st == "copied from p2" and tried == ["p1", "p2"]
    assert (tmp_path / "a/b.iso").read_bytes() == GOOD
    assert not list(tmp_path.glob("a/*.part"))


def test_bad_download_is_never_installed(tmp_path):
    def url(u, part):
        open(part, "wb").write(BAD)
        return True

    st = fim.ensure(_entry(url="https://x/b.iso"), root=str(tmp_path), peers=[], fetch_url=url,
                    fetch_peer=_boom, free_bytes=lambda p: 10**12)
    assert st.startswith("FAIL")
    assert not (tmp_path / "a/b.iso").exists() and not list(tmp_path.glob("a/*.part"))


def test_no_space_fails_before_fetching(tmp_path):
    st = fim.ensure(_entry(url="https://x/b.iso"), root=str(tmp_path), peers=["p1"], fetch_url=_boom,
                    fetch_peer=_boom, free_bytes=lambda p: 10)
    assert st.startswith("FAIL") and "space" in st


@pytest.mark.parametrize("source,want", [("/export/isos/a/b.iso", False), ("/export/isos/a/old.iso", True),
                                         (None, False)])
def test_remount_only_when_mounted_from_another_file(source, want):
    assert fim.needs_remount("/export/isos/a/b.iso", source) is want


def test_role_unmounts_only_the_listed_remounts():
    tasks = yaml.safe_load(open(os.path.join(ROLE, "tasks", "main.yml")))
    umount = [t for t in tasks if "umount" in str(t.get("ansible.builtin.shell", t.get("ansible.builtin.command", "")))]
    assert len(umount) == 1
    assert "_media_out.remount" in str(umount[0]["loop"])
    assert "_media.stdout | from_json" in str(_task("Install-media result"))   # the script's own list


def _tasks():
    return yaml.safe_load(open(os.path.join(ROLE, "tasks", "main.yml")))


def _task(name_part):
    return next(t for t in _tasks() if name_part in t["name"])


def test_check_mode_writes_no_cache(tmp_path, capsys):
    (tmp_path / "a").mkdir()
    (tmp_path / "a/b.iso").write_bytes(GOOD)
    m = tmp_path / "m.json"
    m.write_text(__import__("json").dumps([_entry()]))
    assert fim.main(["--manifest", str(m), "--root", str(tmp_path), "--check"]) == 0
    assert '"present"' in capsys.readouterr().out
    assert not (tmp_path / "a/b.iso.sha256").exists()


def test_role_check_mode_needs_nothing_on_the_bang():
    t = _task("Fetch / verify")
    argv = t["ansible.builtin.command"]["argv"]
    assert "'--manifest', '-'" in argv and "stdin" in t["ansible.builtin.command"]
    assert "_media_script.stat.exists" in str(t.get("when"))


def test_transfers_cannot_hang():
    c = fim.curl_argv("https://x/b.iso", "/p")
    assert "--connect-timeout" in c and "--speed-limit" in c and "--speed-time" in c
    s = fim.peer_argv("flax", "peer", "/export/isos/a/b.iso")
    assert any("ServerAliveInterval" in a for a in s) and any("ConnectTimeout" in a for a in s)
    assert _task("Fetch / verify").get("timeout")


def test_peer_with_a_different_checksum_is_skipped_before_streaming(tmp_path):
    good = hashlib.sha256(GOOD).hexdigest()

    def peer(host, path, part):
        assert host == "p2", "p1 reported a different checksum and must not be streamed"
        open(part, "wb").write(GOOD)
        return True

    st = fim.ensure(_entry(), root=str(tmp_path), peers=["p1", "p2"], fetch_url=_boom, fetch_peer=peer,
                    free_bytes=lambda p: 10**12, peer_sha=lambda h, p: "0" * 64 if h == "p1" else good)
    assert st == "copied from p2"


def test_one_failed_iso_does_not_block_the_other_mounts():
    assert _task("Fetch / verify")["failed_when"] is False
    assert _task("Mount units")["loop"] == "{{ _media_mounts }}"
    assert "_media_out.status" in str(_task("Verified ISOs that get a mount"))   # only verified entries
    fail = _task("Fail on")
    assert "FAIL" in str(fail)
    names = [t["name"] for t in _tasks()]
    assert names.index("Enable + start the mounts") < names.index(fail["name"])


def test_boot_ipxe_isoname_is_the_mounted_iso():
    """The live installer downloads url=http://bang/<isodir>/<isoname>: it must be the very ISO
    whose casper kernel the mount serves, or the kernel and squashfs come from different releases."""
    m = yaml.safe_load(open(MANIFEST))["isos"]
    ipxe = open(os.path.join(_REAPER, "roles/apply_pxe_payloads/templates/v/boot.ipxe.j2")).read()
    blocks = re.findall(r"set isodir isos/(\S+)\nset isoname (\S+)\nset install \$\{install-url\}/(\S+)", ipxe)
    assert blocks
    by_mount = {e["mount"]: e["path"] for e in m if e["mount"]}
    for isodir, isoname, mount in blocks:
        assert by_mount.get(mount) == f"{isodir}/{isoname}", mount


def test_remount_unmounts_every_stacked_mount():
    """edam 2026-11-10: the old ONIE ISO was mounted twice at one target (a May unit + the hourly
    cron); one umount bared the second, systemd saw the path mounted and never mounted the new ISO."""
    t = _task("Unmount targets")
    body = str(t.get("ansible.builtin.shell", t.get("ansible.builtin.command", "")))
    assert "while mountpoint -q" in body and "umount" in body and "exit 1" in body


def test_peers_are_own_site_first_then_the_rest_in_list_order():
    """The try order is stable: this site's other bangs, then every other bang in
    apply_install_media_all_bangs order, each <bang>.<bang_domain>."""
    import jinja2
    import yaml
    d = yaml.safe_load(open(os.path.join(ROLE, "defaults", "main.yml")).read())
    env = jinja2.Environment()
    ctx = {"groups": {"jumphosts": ["bang-gouda", "bang-edam"]}, "inventory_hostname": "bang-gouda",
           "bang_domain": "bangs.example.net", **d}
    ctx["apply_install_media_peer_domain"] = env.from_string(d["apply_install_media_peer_domain"]).render(**ctx)
    from ansible.plugins.filter.core import FilterModule
    env.filters.update(FilterModule().filters())
    peers = env.from_string(d["apply_install_media_peers"]).render(**ctx).strip()
    assert peers == str([f"{b}.bangs.example.net" for b in
                         ("bang-edam", "bang-rhythm", "bang-blues", "bang-fiesta", "bang-siesta")])
