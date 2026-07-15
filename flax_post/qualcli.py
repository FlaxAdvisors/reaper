# flax_post/qualcli.py
"""Operator debug CLI: hit a node agent's REST API directly, pipeline-independent.

    python -m flax_post.qualcli <host_ip> [stage]

Shares qualclient with the producer, so 'what does the node say' is one code path
everywhere. Exit 2 = agent unreachable.
"""
import sys

from .qualclient import QualClient, QualUnreachable


def render(status, stages) -> str:
    head = (f"overall: {status.get('status')} "
            f"({status.get('done_n')}/{status.get('total_n')}, {status.get('pct')}%) "
            f"current={status.get('current')} verdict={status.get('verdict')}")
    rows = [f"  {s['name']:<18} {s['status']}" for s in stages]
    return "\n".join([head, *rows])


def _make_client(host_ip):
    return QualClient(f"http://{host_ip}:8087")


def main(argv, *, make_client=_make_client) -> int:
    if not argv:
        print("usage: python -m flax_post.qualcli <host_ip> [stage]", file=sys.stderr)
        return 1
    client = make_client(argv[0])
    try:
        if len(argv) >= 2:
            detail = client.stage(argv[1])
            print(f"{detail['name']}: {detail['status']}  summary={detail.get('summary')}")
            for a in detail.get("artifacts", []):
                print(f"  {a['name']:<14} {a['kind']:<7} {a['bytes']} bytes")
        else:
            print(render(client.status(), client.stages()))
    except QualUnreachable as e:
        print(f"unreachable: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
