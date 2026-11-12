"""apply_pxe_payloads renders /export/install autoinstall files from vault-backed templates
(tag install_files). Baselines are the live files masked by tools/install_files (secrets ->
<<name>>, a key run -> one <<KEY>> line), so no secret is kept here either.
Spec: docs/superpowers/specs/2026-11-10-install-files-into-reaper-design.md."""
import os
import re

import jinja2
import yaml

_REAPER = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
ROLE = os.path.join(_REAPER, "roles", "apply_pxe_payloads")
TDIR = os.path.join(ROLE, "templates", "install")
KEPT = os.path.join(ROLE, "files", "install-kept")
BASE = os.path.join(os.path.dirname(__file__), "data", "install_baseline")

APT = ("  apt:\n    geoip: false\n    primary:\n      - arches: [default]\n"
       "        uri: http://mirrors.edge.kernel.org/ubuntu\n    security:\n"
       "      - arches: [default]\n        uri: http://mirrors.edge.kernel.org/ubuntu\n"
       "  packages:\n    - stress-ng\n    - rasdaemon\n")


def _files(root, suffix=""):
    out = set()
    for dp, _, fn in os.walk(root):
        for f in fn:
            if f.endswith(suffix):
                out.add(os.path.relpath(os.path.join(dp, f), root)[: len(f) and -len(suffix) or None])
    return out


def _render(text):
    env = jinja2.Environment(trim_blocks=True, keep_trailing_newline=True,
                             undefined=jinja2.StrictUndefined)
    names = set(re.findall(r"vault_install_secrets\.([a-z0-9_]+)", text))
    return env.from_string(text).render(
        vault_install_authorized_keys=["<<KEY>>"],
        vault_install_secrets={n: f"<<{n}>>" for n in names},
        install_flax_hash="<<install_flax_hash>>", install_pxadmin_hash="<<install_pxadmin_hash>>",
        install_flax_password="<<install_flax_password>>",
        install_pxadmin_password="<<install_pxadmin_password>>")


CEC = ("    # ras=cec_disable: the kernel's correctable-error collector otherwise keeps CEs from EDAC,\n"
       "    # so dimmerr reads 0 for a DIMM rasdaemon logged (LVQ et7b4 2026-11-10)\n"
       "    - 'printf \"%s\\n\" ''GRUB_CMDLINE_LINUX=\"$GRUB_CMDLINE_LINUX ras=cec_disable\"'' "
       "> /target/etc/default/grub.d/90-flax-ras.cfg'\n"
       "    - curtin in-target --target=/target -- update-grub\n")


BOOTORDER = r"""    # disk first before the shutdown (spec 2026-11-11-deploy-firstboot-boot-order): AMI
    # FixedBootOrder keeps the network ahead of a new entry; the order goes to the console (SOL)
    - |
      {
      echo "flax: BootOrder after install:"; efibootmgr
      pu=$(findmnt -no PARTUUID /target/boot/efi)
      e=$(efibootmgr -v | grep -i "$pu" | grep -i 'EFI\\ubuntu\\shimx64.efi' | sed -n 's/^Boot\([0-9A-Fa-f]\{4\}\).*/\1/p' | sort | head -n1)
      if [ -n "$pu" ] && [ -n "$e" ]; then
        rest=$(efibootmgr | sed -n 's/^BootOrder: //p' | tr ',' '\n' | grep -vix "$e" | paste -sd, -)
        efibootmgr -o "$e${rest:+,$rest}" >/dev/null
        echo "flax: BootOrder set ubuntu first ($e):"; efibootmgr | sed -n 3p
      else
        echo "flax: no ubuntu entry on ESP ${pu:-unknown}; BootOrder left alone"
      fi
      } > /dev/console 2>&1 || true
"""
TP_UD = ("canonical/ubuntu/24.04/wiwynn-tp/user-data", "canonical/ubuntu/24.04/tiogapass/user-data")


def _reconcile(path, base):
    """The agreed changes (spec: Reconciliations) applied to a user-data baseline: 22.04's
    disk size + apt block, and on 24.04 ras=cec_disable before the closing shutdown."""
    if path.startswith("canonical/ubuntu/24.04/") and path.endswith("/user-data"):
        extra = BOOTORDER if path in TP_UD else ""
        return base.replace("    - shutdown -h now\n", CEC + extra + "    - shutdown -h now\n", 1)
    if not (path.startswith("canonical/ubuntu/22.04/") and path.endswith("/user-data")):
        return base
    base = base.replace("\n          size: 20G,", "\n          size: 100G,")
    if "\n  apt:" not in base:
        i = base.index("\n  locale:")
        j = base.index("\n", i + 1) + 1
        base = base[:j] + APT + base[j:]
    return base


def test_every_managed_file_has_a_template():
    tpl = _files(TDIR, ".j2")
    assert tpl == _files(BASE)
    assert len(tpl) == 62
    assert not any(p.startswith("suse/sles/") or p.startswith("suse/harvester/") for p in tpl)


def test_render_matches_baseline_except_reconciliations():
    bad = []
    for p in sorted(_files(TDIR, ".j2")):
        got = _render(open(os.path.join(TDIR, p + ".j2")).read())
        want = _reconcile(p, open(os.path.join(BASE, p)).read())
        if got != want:
            bad.append(p)
    assert bad == []


def test_no_secret_literal_in_reaper():
    hits = []
    for root in (TDIR, KEPT):
        for p in _files(root):
            t = open(os.path.join(root, p)).read()
            if re.search(r"\$(1|5|6|y)\$[A-Za-z0-9./$]{2,}", t) \
                    or re.search(r"ssh-(rsa|ed25519|dss) [A-Za-z0-9+/]{20,}", t):
                hits.append(p)
            for m in re.finditer(r"passwd/\S+ password (\S+)|^\s*#?\s*rootpw\s+(?:--iscrypted\s+)?(\S+)|"
                                 r"--password=(\S+)|^\s*-?\s*(?:hashed_)?passwd:\s*\"?([^\s\"]+)|"
                                 r"^\s*password:\s*\"?([^\s\"]+)|^\s*token:\s*(\S+)|"
                                 r"<user_password>\s*([^<\s]+)", t, re.M):
                v = next(g for g in m.groups() if g is not None)
                if v.startswith("{{") or v in ("!", "!*", "*", "!!"):
                    continue
                hits.append(f"{p}: {m.group(0)[:20]}")
    assert hits == []


def _install_tasks():
    tasks = yaml.safe_load(open(os.path.join(ROLE, "tasks", "main.yml")))
    return [t for t in tasks if "install_files" in (t.get("tags") or [])]


def _block(name):
    tasks = yaml.safe_load(open(os.path.join(ROLE, "tasks", "main.yml")))
    return next(t for t in tasks if t.get("name", "").startswith(name) and "block" in t)


def test_install_files_render_on_the_controller_then_one_rsync():
    # operator 2026-11-10: a template task per file was ~2 min per bang; now one rsync
    b = _block("Install files under")
    tpl = [t for t in b["block"] if "ansible.builtin.template" in t]
    sync = [t for t in b["block"] if "ansible.posix.synchronize" in t]
    assert len(tpl) == 1 and len(sync) == 1
    assert tpl[0].get("no_log") is True and tpl[0]["delegate_to"] == "localhost"
    assert "_install_stage" in tpl[0]["ansible.builtin.template"]["dest"]
    s = sync[0]["ansible.posix.synchronize"]
    opts = " ".join(s["rsync_opts"])
    assert "apply_pxe_payloads_install_root" in s["dest"] and not s.get("delete")
    for o in ("--files-from=", "--chown=flax:users", "--chmod=D0755,F0644", "--backup", "--suffix="):
        assert o in opts, o
    # the stage holds vaulted values: it is removed whatever happens
    [rm] = b["always"]
    assert rm["ansible.builtin.file"]["state"] == "absent" and "_install_stage" in rm["ansible.builtin.file"]["path"]
    assert all("install_files" in t["tags"] for t in b["block"] + b["always"])


def test_pxe_files_never_touch_the_pxe_root_itself():
    b = _block("Per-vlan PXE files")
    [sync] = [t for t in b["block"] if "ansible.posix.synchronize" in t]
    opts = " ".join(sync["ansible.posix.synchronize"]["rsync_opts"])
    assert "--files-from=" in opts and "--chown=dnsmasq:dnsmasq" in opts   # /srv/pxe stays root:root
    assert b["always"][0]["ansible.builtin.file"]["state"] == "absent"


def test_no_password_hash_filter():
    # the pinned toolchain (ansible-core 2.18, no passlib) cannot run password_hash
    for d in ("tasks", "defaults"):
        assert not re.search(r"\|\s*password_hash\(", open(os.path.join(ROLE, d, "main.yml")).read())


def test_tiogapass_autoinstalls_set_ubuntu_first_before_shutdown():
    for rel in TP_UD:
        got = _render(open(os.path.join(TDIR, rel + ".j2")).read())
        assert "efibootmgr -o" in got and got.index("efibootmgr -o") < got.index("- shutdown -h now")
        cmds = yaml.safe_load(got)["autoinstall"]["late-commands"]
        mine = [c for c in cmds if "findmnt -no PARTUUID /target/boot/efi" in c]
        assert len(mine) == 1 and r"grep -i 'EFI\\ubuntu\\shimx64.efi'" in mine[0]
        assert cmds[-1] == "shutdown -h now"
    leo = _render(open(os.path.join(TDIR, "canonical/ubuntu/24.04/leopard/user-data.j2")).read())
    assert "efibootmgr" not in leo
