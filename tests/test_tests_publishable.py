"""tests/ is headed for the published reaper repo, so it carries no binaries (operator
2026-11-09: git holds recipes + text, blobs live on a bang) and no oversized dumps. A
fixture that needs a binary format (e.g. an .xlsx) is built at test time from a text
fixture, as test_flax_rack_bom_import does."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent
MAX_BYTES = 512 * 1024
SKIP = {"__pycache__", ".pytest_cache"}


def _files():
    return [p for p in ROOT.rglob("*")
            if p.is_file() and not p.is_symlink() and not SKIP & set(p.relative_to(ROOT).parts)]


def test_every_file_is_utf8_text():
    bad = []
    for p in _files():
        b = p.read_bytes()
        try:
            b.decode("utf-8")
        except UnicodeDecodeError:
            bad.append(str(p.relative_to(ROOT)))
            continue
        if b"\x00" in b:
            bad.append(str(p.relative_to(ROOT)))
    assert not bad, f"binary files under tests/ (generate them at test time instead): {bad}"


def test_no_oversized_file():
    big = [(str(p.relative_to(ROOT)), p.stat().st_size) for p in _files()
           if p.stat().st_size > MAX_BYTES]
    assert not big, f"files over {MAX_BYTES} bytes under tests/: {big}"


def test_tests_locate_code_relative_to_this_checkout():
    """A test that reads ~/git/reaper (or a REAPER_ROOT override) checks whatever that
    clone has checked out, not the branch or worktree it runs in, and goes green
    against the wrong tree. A bare relative path ("roles/...") reads whatever tree the
    shell sits in. Locate code from __file__ (parents[1] is the repo root)."""
    import re
    bad_rx = re.compile(r'expanduser\(["\']~/git/reaper|home\(\)[^\n]*"git"|environ[^\n]*"REAPER_ROOT"'
                        r'|(?:Path|open)\(["\'](?:roles|scripts|schema|files|inventories|playbooks'
                        r'|flax_\w+|templates|library|filter_plugins)/')
    me = pathlib.Path(__file__).name
    bad = []
    for p in ROOT.glob("*.py"):
        if p.name == me:
            continue
        for n, line in enumerate(p.read_text().splitlines(), 1):
            if bad_rx.search(line) and not line.lstrip().startswith("#"):
                bad.append("%s:%d" % (p.name, n))
    assert not bad, bad


def test_no_tailnet_address_except_the_synthetic_range():
    """Host addresses in the 100.64.0.0/10 range come from the site vaults
    (bang_<name>_ts_ip); tests and fixtures use 100.64.0.0/24 only (as
    fixtures/reapercli/gouda/tailscale.txt does). Scans every tracked text file."""
    import ipaddress
    import re
    import subprocess
    repo = ROOT.parent
    tracked = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"], capture_output=True,
                             check=True).stdout.decode().split("\0")
    rx = re.compile(r"(?<![\d.])100\.\d{1,3}\.\d{1,3}\.\d{1,3}(?![\d])")
    tailnet, synthetic = ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("100.64.0.0/24")
    bad, seen = [], 0
    for rel in filter(None, tracked):
        p = repo / rel
        if not p.is_file() or p.is_symlink():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue                     # binaries/vault blobs: other checks own those
        for n, line in enumerate(text.splitlines(), 1):
            for m in rx.findall(line):
                try:
                    a = ipaddress.ip_address(m)
                except ValueError:
                    continue
                if a in tailnet:
                    seen += 1
                    if a not in synthetic:
                        bad.append(f"{rel}:{n}")    # location only: never echo the address
    assert seen > 0, "found no tailnet-range address at all (presence proof: the synthetic fixtures carry some)"
    assert not bad, bad


def _tracked_text():
    """(path, text) for every tracked UTF-8 file in the checkout."""
    import subprocess
    repo = ROOT.parent
    tracked = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"], capture_output=True,
                             check=True).stdout.decode().split("\0")
    for rel in filter(None, tracked):
        p = repo / rel
        if not p.is_file() or p.is_symlink():
            continue
        try:
            yield rel, p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue


def _vault_values(name):
    """{site: value of `name`} from every site vault this host can open (in memory)."""
    import os
    import subprocess
    import pytest
    import yaml

    class _Loader(yaml.SafeLoader):
        pass
    _Loader.add_constructor("!vault", lambda loader, node: None)
    repo, out = ROOT.parent, {}
    for site in ("eindhoven", "lavergne", "braintree"):
        if not os.path.exists(os.path.expanduser(f"~/.vault_pass_{site}")):
            continue
        plain = subprocess.run(["ansible-vault", "view", "--vault-password-file",
                                str(repo / "scripts" / "vault-password.sh"),
                                str(repo / "inventories" / site / "group_vars" / "all" / "vault.yml")],
                               capture_output=True, text=True, check=True, cwd=repo,
                               env={**os.environ, "REAPER_SITE": site}).stdout
        out[site] = yaml.load(plain, Loader=_Loader).get(name)
    if not out:
        pytest.skip("no site vault password on this host")
    return out


def test_the_bang_domain_is_only_in_the_vault():
    """bang_domain (vault_bang_domain) is read from the site vaults, never written in a
    tracked file; tests use bangs.example.net. Reports file:line only."""
    domains = {d.lower() for d in _vault_values("vault_bang_domain").values()}
    assert domains and all(domains), "a site vault lacks vault_bang_domain"
    bad = [f"{rel}:{n}" for rel, text in _tracked_text()
           for n, line in enumerate(text.lower().splitlines(), 1) if any(d in line for d in domains)]
    assert not bad, sorted(set(bad))


def test_no_ssh_public_key_in_a_tracked_file():
    """flax's authorized_keys live in the site vaults (vault_flax_authorized_keys). A test
    needing a key uses a short fake ("ssh-ed25519 AAAA x")."""
    import re
    rx = re.compile(r"(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp\d+|sk-ssh-ed25519@openssh\.com) AAAA[A-Za-z0-9+/]{40,}")
    bad = [f"{rel}:{n}" for rel, text in _tracked_text()
           for n, line in enumerate(text.splitlines(), 1) if rx.search(line)]
    assert not bad, bad


def test_every_site_vault_carries_flax_authorized_keys():
    for site, keys in _vault_values("vault_flax_authorized_keys").items():
        assert keys and all(k.split()[0].startswith(("ssh-", "ecdsa-", "sk-")) for k in keys), site
