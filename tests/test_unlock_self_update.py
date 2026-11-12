"""A station updates itself from the bang before the flash pass runs.

Before this, upgrading a deployed station meant booting it with a known-good
NIC, or a GRUB edit adding systemd.mask=mezz-flash.service, then scp +
deploy.sh. Now mezz-flash-update.service (oneshot, ordered before
mezz-flash.service) compares the bundle version it runs against
http://<gateway>/mezz-flash.version and pulls mezz-flash.tgz only when the two
differ. See docs/superpowers/specs/2026-09-24-mezz-flash-self-update-design.md.

The updater must NEVER stop a flash run: every failure path exits 0 and leaves
the installed tree exactly as it was.

These run the REAL self_update.sh and deploy.sh under a fake root
(MEZZ_ROOT), with fake ip/curl/systemctl/dpkg/etc. on PATH.
"""
import hashlib
import os
import pathlib
import shutil
import subprocess
import tarfile

import pathlib as _pl
REAPER = _pl.Path(__file__).resolve().parents[1]
PAYLOAD = REAPER / "scripts" / "unlock-with-ubuntu" / "payload"

_COMMON = (PAYLOAD.parent.parent.parent / "roles" / "apply_pxe_payloads"
           / "files" / "post" / "common_mellanox.sh")


_OLD = "aaaaaaa@20260901T000000Z"
_NEW = "bbbbbbb@20260924T120000Z"

# Every fake logs "<name> <args>" to calls.log. curl serves files out of
# $FAKEDIR/srv by URL basename, or fails when $FAKEDIR/curl_fails exists.
_FAKES = {
    "ip": 'cat "$FAKEDIR/route" 2>/dev/null\n',
    "curl": r'''
out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift ;;
    http*) url="$1" ;;
  esac
  shift
done
[ -e "$FAKEDIR/curl_fails" ] && exit 7
f="$FAKEDIR/srv/$(basename "$url")"
[ -f "$f" ] || exit 22
if [ -n "$out" ]; then cp "$f" "$out"; else cat "$f"; fi
''',
    "dpkg": '[ -e "$FAKEDIR/pkgs_missing" ] && exit 1\nexit 0\n',
    "id": 'echo 0\n',
    "findmnt": 'echo /dev/fake\n',
    "sleep": 'exit 0\n',
}
_NOOPS = ("systemctl", "agetty", "apt-get", "modprobe", "ipmitool",
          "tune2fs", "update-grub")

_IDENT = ('#!/bin/sh\necho \'{"station_sn":"QCFF06191403C6",'
          '"serial_field":"Chassis Serial","family":"leopard","state":"ok"}\'\n')


class Station:
    def __init__(self, tmp, local_manifest="bundle : unlock-with-ubuntu\n"
                                           "version : %s\n" % _OLD):
        self.tmp = pathlib.Path(tmp)
        self.root = self.tmp / "root"
        self.fake = self.tmp / "fake"
        self.bin = self.fake / "bin"
        self.srv = self.fake / "srv"
        for d in (self.root, self.bin, self.srv):
            d.mkdir(parents=True)
        for name, body in _FAKES.items():
            self._fake(name, body)
        for name in _NOOPS:
            self._fake(name, "exit 0\n")
        self.route("default via 172.16.9.1 dev eth0 proto dhcp\n")
        self.netdevs("lo", "eth0")
        # The installed station: the real payload plus a MANIFEST.
        self.dst = self.root / "opt" / "flax" / "mezzflash"
        self._payload_into(self.dst, local_manifest)
        (self.dst / "deploy.sh").unlink()     # deploy.sh removes itself

    def _fake(self, name, body):
        p = self.bin / name
        p.write_text('#!/bin/bash\necho "%s $*" >> "$FAKEDIR/calls.log"\n%s'
                     % (name, body))
        p.chmod(0o755)

    def _payload_into(self, d, manifest):
        shutil.copytree(PAYLOAD, d, ignore=shutil.ignore_patterns("__pycache__"))
        # The build adds this from the post lane; the payload dir lacks it.
        shutil.copy(_COMMON, d / "common_mellanox.sh")
        (d / "MANIFEST").write_text(manifest)
        (d / "station_ident.py").write_text(_IDENT)
        (d / "station_ident.py").chmod(0o755)

    def netdevs(self, *names):
        net = self.root / "sys" / "class" / "net"
        shutil.rmtree(net, ignore_errors=True)
        net.mkdir(parents=True)
        for n in names:
            (net / n).mkdir()

    def route(self, text):
        (self.fake / "route").write_text(text)

    def publish(self, version=_NEW, sha=None, bundle_version=None):
        """Put mezz-flash.tgz + mezz-flash.version on the fake bang."""
        build = self.tmp / "build"
        shutil.rmtree(build, ignore_errors=True)
        self._payload_into(build, "bundle : unlock-with-ubuntu\nversion : %s\n"
                           % (bundle_version or version))
        (build / "NEW_MARKER").write_text("from the new bundle\n")
        tgz = self.srv / "mezz-flash.tgz"
        with tarfile.open(tgz, "w:gz") as t:
            t.add(build, arcname=".")
        digest = sha or hashlib.sha256(tgz.read_bytes()).hexdigest()
        (self.srv / "mezz-flash.version").write_text(
            "version=%s\nsha256=%s\nsize=%d\n" % (version, digest, tgz.stat().st_size))

    def run(self, route_wait="2"):
        env = dict(os.environ, PATH="%s:%s" % (self.bin, os.environ["PATH"]),
                   FAKEDIR=str(self.fake), MEZZ_ROOT=str(self.root),
                   MEZZ_ROUTE_WAIT=route_wait)
        self.proc = subprocess.run(["bash", str(self.dst / "self_update.sh")],
                                   capture_output=True, text=True, env=env)
        return self.proc

    def calls(self, name=None):
        f = self.fake / "calls.log"
        lines = f.read_text().splitlines() if f.exists() else []
        return [l for l in lines if name is None or l.split(" ", 1)[0] == name]

    def fetched(self, basename):
        return [c for c in self.calls("curl") if basename in c]

    def banner(self):
        f = self.root / "etc" / "issue.d" / "mezz-flash-update.issue"
        return f.read_text() if f.exists() else ""

    def installed_version(self):
        for line in (self.dst / "MANIFEST").read_text().splitlines():
            if line.startswith("version"):
                return line.split()[-1]
        return None

    def why(self):
        return "rc=%s\nstdout:\n%s\nstderr:\n%s\ncalls:\n%s\nbanner:\n%s" % (
            self.proc.returncode, self.proc.stdout, self.proc.stderr,
            "\n".join(self.calls()), self.banner())


# ---- is an update required? ------------------------------------------------

def test_same_version_downloads_nothing(tmp_path):
    s = Station(tmp_path)
    s.publish(version=_OLD)
    s.run()
    assert s.fetched("mezz-flash.version"), s.why()     # it did check
    assert not s.fetched("mezz-flash.tgz"), s.why()
    assert s.proc.returncode == 0, s.why()
    assert "current" in s.banner() and _OLD in s.banner(), s.why()


def test_a_newer_published_version_is_installed(tmp_path):
    s = Station(tmp_path)
    s.publish()
    s.run()
    assert s.proc.returncode == 0, s.why()
    assert s.installed_version() == _NEW, s.why()
    assert (s.dst / "NEW_MARKER").exists(), s.why()
    assert "updated" in s.banner() and _OLD in s.banner() and _NEW in s.banner(), s.why()


def test_the_previous_tree_is_kept_for_rollback(tmp_path):
    s = Station(tmp_path)
    s.publish()
    s.run()
    prev = s.dst.parent / "mezzflash.prev"
    assert (prev / "MANIFEST").exists(), s.why()
    assert _OLD in (prev / "MANIFEST").read_text(), s.why()


def test_a_station_installed_before_versioning_updates_once(tmp_path):
    s = Station(tmp_path, local_manifest="bundle : unlock-with-ubuntu\nrepo   : 9ee9119\n")
    s.publish()
    s.run()
    assert s.installed_version() == _NEW, s.why()


def test_the_gateway_is_asked_not_the_vip_name(tmp_path):
    # Same reason as pushstatus: a free-pool lease may not route to the VIP
    # that `bang` resolves to, while the bang is the gateway on every VLAN.
    s = Station(tmp_path)
    s.publish(version=_OLD)
    s.run()
    assert "http://172.16.9.1/mezz-flash.version" in s.fetched("mezz-flash.version")[0], s.why()


# ---- the update must never stop a flash run ----------------------------------

def test_no_route_means_no_fetch_and_a_normal_exit(tmp_path):
    # A jumpered card never gets a lease: the bounded wait is all it costs.
    s = Station(tmp_path)
    s.route("")
    s.publish()
    s.run()
    assert s.proc.returncode == 0, s.why()
    assert not s.calls("curl"), s.why()
    assert s.installed_version() == _OLD, s.why()
    assert "no network" in s.banner(), s.why()


def test_a_station_with_only_lo_skips_the_route_wait(tmp_path):
    # A jumpered (livefish) card creates no netdev: only lo exists, and no
    # route can ever appear. Skip after a short udev grace, not the full wait.
    s = Station(tmp_path)
    s.netdevs("lo")
    s.route("")
    s.publish()
    s.run(route_wait="20")
    assert s.proc.returncode == 0, s.why()
    assert len(s.calls("sleep")) <= 3, s.why()
    assert not s.calls("curl"), s.why()
    assert "no network" in s.banner() and "no NIC" in s.banner(), s.why()


def test_any_nic_counts_not_just_mellanox(tmp_path):
    # A dev node's PCIe uplink (any vendor) must still reach the bang while
    # the jumpered mezz card is in.
    s = Station(tmp_path)
    s.netdevs("lo", "enp1s0f0")
    s.publish()
    s.run()
    assert s.installed_version() == _NEW, s.why()


def test_a_nic_without_a_route_still_gets_the_full_wait(tmp_path):
    s = Station(tmp_path)
    s.route("")
    s.run(route_wait="5")
    assert len(s.calls("sleep")) >= 5, s.why()


def test_an_unreachable_bang_leaves_the_station_alone(tmp_path):
    s = Station(tmp_path)
    s.publish()
    (s.fake / "curl_fails").touch()
    s.run()
    assert s.proc.returncode == 0, s.why()
    assert s.installed_version() == _OLD, s.why()
    assert "check failed" in s.banner() and _OLD in s.banner(), s.why()


def test_a_checksum_mismatch_installs_nothing(tmp_path):
    s = Station(tmp_path)
    s.publish(sha="0" * 64)
    s.run()
    assert s.fetched("mezz-flash.tgz"), "never downloaded: " + s.why()
    assert s.proc.returncode == 0, s.why()
    assert s.installed_version() == _OLD, s.why()
    assert not (s.dst / "NEW_MARKER").exists(), s.why()
    assert not s.calls("systemctl"), s.why()
    assert "sha256" in s.banner(), s.why()


def test_a_bundle_whose_manifest_disagrees_is_refused(tmp_path):
    # The version file and the tarball must describe the same build, or the
    # station reinstalls on every boot and never converges.
    s = Station(tmp_path)
    s.publish(version=_NEW, bundle_version="ccccccc@20260924T130000Z")
    s.run()
    assert s.proc.returncode == 0, s.why()
    assert s.installed_version() == _OLD, s.why()
    assert "check failed" in s.banner(), s.why()


def test_missing_packages_refuse_the_update_instead_of_apt(tmp_path):
    # Inside the boot path there is no mirror to reach; never run apt there.
    s = Station(tmp_path)
    (s.fake / "pkgs_missing").touch()
    s.publish()
    s.run()
    assert s.proc.returncode == 0, s.why()
    assert not s.calls("apt-get"), s.why()
    assert s.installed_version() == _OLD, s.why()
    assert "install failed" in s.banner(), s.why()


# ---- deploy.sh --update ----------------------------------------------------

def test_update_mode_never_runs_apt(tmp_path):
    s = Station(tmp_path)
    s.publish()
    s.run()
    assert s.installed_version() == _NEW, s.why()      # the path really ran
    assert not s.calls("apt-get"), s.why()


def test_update_mode_starts_no_unit(tmp_path):
    # mezz-flash-serial.service is After=mezz-flash.service, which is After=
    # the updater: a blocking start from inside the updater deadlocks boot.
    s = Station(tmp_path)
    s.publish()
    s.run()
    sc = s.calls("systemctl")
    assert any("daemon-reload" in c for c in sc), s.why()
    bad = [c for c in sc if "--now" in c or c.split()[1:2] in (["start"], ["restart"])]
    assert not bad, bad


def test_update_mode_installs_the_updater_unit(tmp_path):
    s = Station(tmp_path)
    s.publish()
    s.run()
    unit = s.root / "etc" / "systemd" / "system" / "mezz-flash-update.service"
    assert unit.exists(), s.why()


# ---- units ----------------------------------------------------------------

def _unit(name):
    return (PAYLOAD / name).read_text()


def test_the_flash_pass_waits_for_the_updater_but_does_not_require_it():
    svc = _unit("mezz-flash.service")
    after = [l for l in svc.splitlines() if l.startswith("After=")]
    assert any("mezz-flash-update.service" in l for l in after), svc
    wants = [l for l in svc.splitlines() if l.startswith("Wants=")]
    assert any("mezz-flash-update.service" in l for l in wants), svc
    assert not [l for l in svc.splitlines()
                if l.startswith("Requires=") and "mezz-flash-update" in l], svc


def test_the_updater_is_a_bounded_oneshot():
    svc = _unit("mezz-flash-update.service")
    assert "Type=oneshot" in svc, svc
    assert "TimeoutStartSec=" in svc, svc
    assert "self_update.sh" in svc, svc


def test_the_updater_does_not_hold_multi_user_target():
    # Pulled in only by the flash pass's Wants=; WantedBy=multi-user.target
    # would make that target wait out the whole check on every boot.
    unit = _unit("mezz-flash-update.service")
    assert not [l for l in unit.splitlines() if l.startswith("WantedBy=")], unit


# ---- build / publish --------------------------------------------------------

_BUILD = PAYLOAD.parent / "create-unlock-with-ubuntu.sh"
_PUBLISH = PAYLOAD.parent / "publish_bundle.sh"


def test_the_bundle_manifest_carries_a_version():
    assert "version :" in _BUILD.read_text()


def test_publish_writes_a_version_file_matching_the_tarball(tmp_path):
    tgz = tmp_path / "unlock-with-ubuntu.tgz"
    tgz.write_bytes(b"not really a tarball, but bytes to hash\n")
    out = tmp_path / "srv"
    out.mkdir()
    r = subprocess.run(["bash", str(_PUBLISH), str(tgz), _NEW, str(out)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    pub = out / "mezz-flash.tgz"
    assert pub.read_bytes() == tgz.read_bytes()
    kv = dict(l.split("=", 1) for l in
              (out / "mezz-flash.version").read_text().splitlines() if "=" in l)
    assert kv["version"] == _NEW
    assert kv["sha256"] == hashlib.sha256(tgz.read_bytes()).hexdigest()
    assert kv["size"] == str(tgz.stat().st_size)
    assert not list(out.glob("*.tmp*")), list(out.iterdir())


def test_publish_writes_the_version_file_last():
    # A station reading a new version before its tgz is complete would fail
    # the checksum; the order is the whole guarantee, so pin it.
    text = _PUBLISH.read_text()
    assert text.rindex("mezz-flash.tgz") < text.rindex("mezz-flash.version"), text
