"""reaper test: subsystem groups -> test files (pytest under tests/, shell suites under scripts/).

A group is a set of globs relative to the repo root. A word that names an existing path is
taken as itself. No words = every test file and every shell suite."""
from pathlib import Path

SERVICES = ("post", "rack", "rungs", "control", "classify", "reconcile", "observe", "switch", "discover")
VENV_BUILDER = "scripts/test-venv.sh"     # builds the test venv; not a test suite

GROUPS = {
    **{s: (f"tests/test_flax_{s}_*.py",) for s in SERVICES},
    "post": ("tests/test_flax_post_*.py", "tests/test_post_*.py"),
    "flax": ("tests/test_flax_*.py",),    # direct glob, so a new test_flax_<svc>_ service is never missed
    "cli": ("tests/test_reapercli_*.py",),
    "roles": ("tests/test_apply_*.py", "tests/test_roles_*.py"),
    "migrations": ("tests/test_migration_*.py", "tests/test_schema_*.py"),
    "unlock": ("tests/test_unlock_*.py",),
    "health": ("tests/test_health_*.py", "tests/test_healthboard_*.py"),
    "secrets": ("tests/test_redact_secrets.py", "tests/test_check_secrets.py", "scripts/test-no-procsub-creds.sh"),
    "repo": ("tests/test_tests_publishable.py", "tests/test_setup_*.py", "tests/test_ssh_*.py",
             "tests/test_reaper_toolchain.py"),
    "shell": ("scripts/test-*.sh",),
}
ALIASES_COMPOSITE = {"post-lane": ("post", "rack", "rungs")}


class GroupError(Exception):
    pass


def group_names() -> list:
    return sorted({*GROUPS, *ALIASES_COMPOSITE})


def _globs(root: Path, globs) -> set:
    return {p for g in globs for p in root.glob(g) if p.is_file() and p.relative_to(root).as_posix() != VENV_BUILDER}


def _split(root: Path, paths) -> tuple:
    def kind(p):
        return Path(str(p).split("::")[0]).suffix
    files = sorted(p for p in paths if kind(p) == ".py")
    shell = sorted(p for p in paths if kind(p) == ".sh")
    return files, shell


def resolve(root: Path, words: list) -> tuple:
    """(pytest files, shell suites), each sorted and de-duplicated."""
    if not words:
        return _split(root, _globs(root, ("tests/test_*.py", "scripts/test-*.sh")))
    out = set()
    for w in words:
        if w in GROUPS or w in ALIASES_COMPOSITE:
            hit = _globs(root, [g for m in ALIASES_COMPOSITE.get(w, (w,)) for g in GROUPS[m]])
            if not hit:
                raise GroupError(f"group {w!r} matches no files under {root} (stale pattern?)")
            out |= hit
        elif (root / w.split("::")[0]).is_dir():
            d = (root / w).relative_to(root).as_posix()
            hit = _globs(root, (f"{d}/test_*.py", f"{d}/test-*.sh"))
            if not hit:
                raise GroupError(f"{w!r} holds no test_*.py or test-*.sh")
            out |= hit
        elif (root / w.split("::")[0]).exists():
            if Path(w.split("::")[0]).suffix not in (".py", ".sh"):
                raise GroupError(f"{w!r} is not a test file (.py or .sh)")
            out.add(root / w)
        else:
            raise GroupError(f"unknown group {w!r}; groups: {' '.join(group_names())}")
    return _split(root, out)


def ungrouped(root: Path) -> list:
    every = _globs(root, ("tests/test_*.py",))
    grouped = _globs(root, [g for globs in GROUPS.values() for g in globs])
    return sorted(every - grouped)
