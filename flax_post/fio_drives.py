"""Every drive's fio numbers as post_fio_drive rows (migration 043).

rows() turns one run's stored `fio` + `inventory/alldisks` artifacts into one row per
drive x job x metric, keyed the way flax_post.fio_judge keys siblings (model, size).
`python -m flax_post.fio_drives backfill` writes every stored run (one-off, idempotent)."""
import sys

from flax_rungs import fio as fio_rung

from . import fio_judge


def rows(fio_text, alldisks) -> list[dict]:
    info = fio_rung.disk_info(alldisks)
    out = []
    for dev, job, text in fio_rung.sections(fio_text or ""):
        if job not in fio_judge.METRICS:
            continue
        pj = fio_rung.parse_job(text)
        d = info.get(dev) or {}
        model, size = fio_judge.drive_key(d, dev)
        for metric, _good in fio_judge.METRICS[job]:
            v = fio_judge._get(pj, metric)
            if v is None:
                continue
            out.append({"dev": dev, "serial": d.get("serial"), "model": model, "size_gb": size,
                        "controller": d.get("controller"), "job": job, "metric": metric,
                        "value": float(v)})
    return out


def backfill(store=None) -> tuple[int, int]:
    """Write every stored run that has a fio artifact; returns (runs, rows)."""
    if store is None:
        from . import state as store
    runs = n = 0
    for bmc_mac, run_id in store.fio_runs():
        r = rows(store.get_artifact(bmc_mac, run_id, "fio", "fio"),
                 store.get_artifact(bmc_mac, run_id, "inventory", "alldisks"))
        if r:
            n += store.write_fio_drives(bmc_mac, run_id, r)
            runs += 1
    return runs, n


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv != ["backfill"]:
        print("usage: python -m flax_post.fio_drives backfill", file=sys.stderr)
        return 2
    runs, n = backfill()
    print(f"runs {runs} rows {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
