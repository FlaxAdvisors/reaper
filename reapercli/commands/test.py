"""reaper test|tests [group...]: run this tree's tests by subsystem group (spec 2026-11-13).

Local only. pytest goes through scripts/run-tests.sh (the one owner of the test venv), in
parallel by default: pass A `-n auto -m "not serial"`, pass B `-m serial` in one process.
Shell suites (scripts/test-*.sh) run concurrently in a thread pool."""
import argparse
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from reapercli.lib import testgroups as tg

NO_TESTS = 5                                   # pytest: nothing collected (an empty serial pass)
# passthrough that takes the run's mode over: -n/-m in any form (-n 4, -n4, -mslow; no other
# pytest option starts with -n or -m), --numprocesses, --pdb, and xdist switched off
def _takes_over(passthrough) -> bool:
    for i, x in enumerate(passthrough):
        nxt = passthrough[i + 1] if i + 1 < len(passthrough) else ""
        if x == "--pdb" or x.startswith(("-n", "-m", "--numprocesses")):
            return True
        if x.startswith("-p") and "no:xdist" in (x[2:] or nxt):
            return True
    return False


def _default_runner(root):
    def run(argv, capture):
        r = subprocess.run(argv, cwd=root, capture_output=capture, text=True)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")) if capture else ""
    return run


def _header(root) -> str:
    def git(*a):
        return subprocess.run(["git", "-C", str(root), *a], capture_output=True, text=True).stdout.strip()
    try:
        branch, sha = git("rev-parse", "--abbrev-ref", "HEAD"), git("rev-parse", "--short", "HEAD")
    except OSError:
        branch = sha = ""
    return f"testing {root} ({branch} @ {sha})" if sha else f"testing {root}"


def _list(root, groups) -> None:
    if groups:
        files, shell = tg.resolve(root, groups)
        for p in [*files, *shell]:
            print(p.relative_to(root) if "::" not in str(p) else p)
        return
    for name in tg.group_names():
        files, shell = tg.resolve(root, [name])
        print(f"  {name:<12} {len(files) + len(shell)} files")
    print("(ungrouped)")
    for p in tg.ungrouped(root):
        print(f"  {p.relative_to(root)}")


def _pytest(root, run, files, a, passthrough) -> int:
    script = str(root / "scripts" / "run-tests.sh")
    base = [script, *map(str, files)]
    if a.serial or _takes_over(passthrough):
        return run([*base, *passthrough], False)[0]
    rc_a = run([*base, *passthrough, "-n", a.jobs or "auto", "-m", "not serial"], False)[0]
    rc_b = run([*base, *passthrough, "-m", "serial"], False)[0]
    print(f"pytest: parallel pass rc {rc_a}, serial pass rc {rc_b}")
    # NO_TESTS from one pass is fine (nothing serial-marked, or only serial tests picked);
    # from both, nothing ran at all
    return 0 if {rc_a, rc_b} <= {0, NO_TESTS} and 0 in (rc_a, rc_b) else 1


def _shell(root, run, suites, a) -> int:
    failed = []
    workers = 1 if a.serial else (os.cpu_count() or 4)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(run, ["bash", str(s)], True): s for s in suites}
        for f in as_completed(futs):
            rc, out = f.result()
            name = futs[f].name
            if rc == 0:
                print(f"ok   {name}")
                continue
            failed.append(name)
            print(f"FAIL {name} (rc {rc})")
            lines = out.splitlines()
            print("\n".join(lines if a.verbose else lines[-20:]))
    print(f"shell: {len(suites) - len(failed)}/{len(suites)} ok" + (f", FAIL {' '.join(sorted(failed))}" if failed else ""))
    return 1 if failed else 0


def run(argv: list, ctx) -> int:
    from reapercli.lib.sites import load_sites
    left, passthrough = (argv[:argv.index("--")], argv[argv.index("--") + 1:]) if "--" in argv else (argv, [])
    p = argparse.ArgumentParser(prog="reaper test")
    p.add_argument("groups", nargs="*")
    p.add_argument("--list", action="store_true")
    p.add_argument("--shell", action="store_true", help="only the scripts/test-*.sh suites")
    p.add_argument("--no-shell", action="store_true", help="only pytest")
    p.add_argument("-j", dest="jobs", help="pytest workers (default auto)")
    p.add_argument("--serial", action="store_true", help="one process (needed for --pdb)")
    p.add_argument("-v", dest="verbose", action="store_true", help="full output of failing shell suites")
    a = p.parse_args(left)
    root = ctx.root
    sites = {"all", *load_sites(root)}
    if any(g in sites for g in a.groups):
        print("reaper test runs locally; it never touches a bang (drop the site name)", file=sys.stderr)
        return 2
    print(_header(root))
    try:
        if a.list:
            _list(root, a.groups)
            return 0
        files, shell = tg.resolve(root, a.groups)
    except tg.GroupError as e:
        print(f"reaper test: {e}", file=sys.stderr)
        return 2
    runner = ctx.test_runner or _default_runner(root)
    rc = 0
    if files and not a.shell:
        rc |= _pytest(root, runner, files, a, passthrough)
    if shell and not a.no_shell:
        rc |= _shell(root, runner, shell, a)
    return 1 if rc else 0
