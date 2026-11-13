"""Entry point: look only at the subcommand name, then import just that module."""
import getpass
import importlib
import os
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

COMMANDS = {
    "doctor": "reapercli.commands.doctor",
    "status": "reapercli.commands.status",
    "report": "reapercli.commands.report",
    "ha": "reapercli.commands.ha",
    "svc": "reapercli.commands.svc",
    "image": "reapercli.commands.image",
    "deploy": "reapercli.commands.deploy",
    "ipxe": "reapercli.commands.ipxe",
    "migrate": "reapercli.commands.migrate",
    "completion": "reapercli.commands.completion",
    "diag": "reapercli.commands.diag",
    "net": "reapercli.commands.net",
    "fw": "reapercli.commands.fw",
    "test": "reapercli.commands.test",
    "tests": "reapercli.commands.test",
}
# the switch / jump-host subcommands are also accepted bare: `reaper push …` == `reaper net push …`
NET_ALIASES = ("fetch", "drift", "plan", "push", "setup")

USAGE = """usage: reaper <command> [site|all] [args...]

  doctor      toolchain, vault and clone readiness (doctor all: every bang)
  status      one-screen health of every site in scope
  report      full health and status of everything reaper manages, to a file
  ha          status | check | quiesce | arm | keepalived-roll | reseed
  svc         ls | logs | restart
  image       ls
  deploy      <component|flax_core...> [--yes] [--branch B] [--restart-stale]
  ipxe        [--cached] [--yes]: build + install the TFTP NBPs (never part of a deploy)
  migrate     [--tag T] [--yes]
  completion  print bash completion
  diag        [service...] [--since T] [--window L]: a read-only bundle for a first look
  net         fetch | drift | plan | push | setup   (also accepted bare; setup = git hooks + ssh jump hosts + shell env)
  fw          bmc|bios <family> <tar> [--yes]: move a family's firmware target (triage + post)
              status [bmc|bios] [<family>]: reaper's targets vs each bang's manifests + reader starts
  test        [group...] [--list] [--shell|--no-shell] [-j N|--serial] [-- pytest args] (alias: tests)
  shell       enter the pinned toolchain env (reaper shell [cmd...]); leave with exit
"""


@dataclass
class Context:
    root: Path
    home: Path
    hostname: str
    runner: Optional[Callable] = None        # ssh runner (lib.remote.Runner); tests inject a fake
    play_runner: Optional[Callable] = None   # ansible-playbook runner (argv, env) -> rc; tests inject
    user: str = field(default_factory=getpass.getuser)   # who ran it (deploy records)
    configs: Optional[Path] = None           # files/configs (device config, net state); tests inject a tmp dir
    clone: Optional[Path] = None             # the clone guarded/recorded; default root (tests inject)
    sleep: Callable = time.sleep             # waits (ha keepalived-roll); tests inject a no-op
    test_runner: Optional[Callable] = None   # reaper test: (argv, capture) -> (rc, output); tests inject


def default_context() -> Context:
    root = Path(os.environ.get("REAPER_ROOT") or Path(__file__).resolve().parent.parent)
    return Context(root=root, home=Path.home(), hostname=socket.gethostname())


def main(argv: list, ctx: Optional[Context] = None) -> int:
    ctx = ctx or default_context()
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE, file=sys.stderr)
        return 2 if not argv else 0
    name, rest = argv[0], argv[1:]
    if name in NET_ALIASES:
        name, rest = "net", argv
    if name not in COMMANDS:
        print(f"reaper: unknown command {name!r}\n\n{USAGE}", file=sys.stderr)
        return 2
    from reapercli.lib.guards import Refused
    from reapercli.lib.sites import ScopeError
    try:
        return importlib.import_module(COMMANDS[name]).run(rest, ctx)
    except ScopeError as e:
        print(f"reaper: {e}", file=sys.stderr)
        return 2
    except Refused as e:
        print(f"reaper: refused: {e}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("reaper: interrupted (see the lines above for what ran)", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
