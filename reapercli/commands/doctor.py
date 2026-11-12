"""reaper doctor [site|all]: is this machine (or, with a scope, every bang) ready
and on the pinned toolchain? (spec §3.1)

  reaper doctor          this machine: toolchain vs pins, vault files, the clone
  reaper doctor all      every bang in scope: toolchain vs pins; docker/compose/buildx equal
"""
import os
import re
import subprocess
import sys

import yaml

from reapercli.lib.gather import gather
from reapercli.lib.out import Row, exit_code, render
from reapercli.lib.sites import load_sites, resolve_scope

BUILD_TOOLS = ("docker", "compose", "buildx")


def pins(root) -> dict:
    # the interpreter pin lives in one place, the builder's PYVER (compared major.minor:
    # a bang's distro python312 takes patch updates)
    m = re.search(r"^PYVER=([\d.]+)", (root / "scripts" / "toolchain-venv.sh").read_text(), re.M)
    out = {"python": ".".join(m.group(1).split(".")[:2]) if m else "?"}
    for line in (root / "requirements-ansible.txt").read_text().splitlines():
        m = re.match(r"^(ansible-core|PyYAML|Jinja2|ansible-pylibssh|paramiko)==([\w.]+)", line.strip())
        if m:
            out[m.group(1)] = m.group(2)
    for c in yaml.safe_load((root / "requirements.yml").read_text())["collections"]:
        out[c["name"]] = str(c["version"])
    return out


def compare(found: dict, pinned: dict, host: str = "", site: str = "") -> list:
    rows = []
    for name, want in pinned.items():
        have = found.get(name)
        same = have is not None and (have.split(".")[:2] == want.split(".")[:2] if name == "python" else have == want)
        rows.append(Row("toolchain", name, "ok" if same else "FAIL",
                        have if same else f"found {have or 'nothing'}, pin {want}", host, site))
    extra = sorted(k for k in found if "." in k and k not in pinned and k[0].islower() and not k[0].isdigit())
    if extra:
        rows.append(Row("toolchain", "other collections", "info", f"{len(extra)} unpinned (distro bundle)", host, site))
    return rows


def venv_row(venv: str, host: str = "", site: str = "") -> Row:
    """Which python reaper runs on: the pinned venv, or the bare system toolchain."""
    if venv.startswith("/"):
        return Row("toolchain", "venv", "ok", venv, host, site)
    fix = "reaper deploy <site> reaper_toolchain" if host else "./setup"
    return Row("toolchain", "venv", "warn", f"{venv or 'none'}; run: {fix}", host, site)


def _out(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def local_rows(ctx) -> list:
    found = {"python": ".".join(map(str, sys.version_info[:3])), "PyYAML": yaml.__version__}
    m = re.search(r"core ([\d.]+)", _out(["ansible", "--version"]))
    if m:
        found["ansible-core"] = m.group(1)
    import importlib.metadata as md                 # the python reaper runs on (the venv)
    for dist in ("Jinja2", "ansible-pylibssh", "paramiko"):
        try:
            found[dist] = md.version(dist)
        except md.PackageNotFoundError:
            pass
    found.update(dict(re.findall(r"^([a-z_]+\.[a-z_]+)\s+([\d.]+)\s*$",
                                 _out(["ansible-galaxy", "collection", "list"]), re.M)))
    rows = [venv_row(os.environ.get("VIRTUAL_ENV", ""))] + compare(found, pins(ctx.root))
    for s in load_sites(ctx.root).values():
        f = ctx.home / f".vault_pass_{s.name}"
        size = f.stat().st_size if f.exists() else 0
        rows.append(Row("vault", s.name, "ok" if size else "info",
                        f"{f.name} present ({size} bytes)" if size else "not on this machine"))
    git = ["git", "-C", str(ctx.root)]
    branch = _out(git + ["branch", "--show-current"]).strip()
    dirty = [l for l in _out(git + ["status", "--porcelain"]).splitlines() if l.strip()]
    ahead = _out(git + ["rev-list", "@{u}..HEAD"]).split()
    rows += [Row("clone", "branch", "ok" if branch == "main" else "warn", branch or "detached"),
             Row("clone", "clean", "ok" if not dirty else "warn", "clean" if not dirty else f"{len(dirty)} changed"),
             Row("clone", "pushed", "ok" if not ahead else "warn",
                 "HEAD is on origin" if not ahead else f"{len(ahead)} commit(s) not pushed")]
    return rows


def fleet_rows(sites, ctx) -> list:
    rows, seen = [], {}
    pinned = pins(ctx.root)
    for v in gather(sites, ctx, toolchain=True):
        for b in v.hosts.bangs:
            f = v.facts.get(b.name, {})
            if f.get("dormant") or "error" in f:
                rows.append(Row("toolchain", "reach", "dormant" if f.get("dormant") else "FAIL",
                                f.get("error", "dormant"), b.name, v.site.name))
                continue
            tc = f.get("toolchain") or {}
            rows += [venv_row(tc.get("venv", ""), b.name, v.site.name)] + compare(tc, pinned, b.name, v.site.name)
            for tool in BUILD_TOOLS:
                seen.setdefault(tool, {})[f"{v.site.name}/{b.name}"] = tc.get(tool, "")
    for tool, by_bang in seen.items():
        same = len(set(by_bang.values())) == 1
        rows.append(Row("build", tool, "ok" if same else "FAIL",
                        next(iter(by_bang.values())) if same else ", ".join(f"{k}={v}" for k, v in by_bang.items())))
    return rows


def run(argv: list, ctx) -> int:
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        raise SystemExit(0)
    if not argv:
        rows = local_rows(ctx)
    else:
        sites, _ = resolve_scope(argv, ctx.root, ctx.home)
        rows = fleet_rows(sites, ctx)
    print(render(rows))
    return exit_code(rows)
