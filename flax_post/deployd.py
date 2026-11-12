"""flax-post-deployd: Loose Post's Deploy runner (`python3.12 -m flax_post.deployd`).

Runs on the bang HOST (not in the flax-control image): it needs what rackpost's Deploy
needs -- /srv/pxe/ipxe (hostname files), /export/install, the dnsmasq/nginx logs,
ansible-playbook + /opt/flax/ansible, ipmitool, sshpass -- plus post's DB (psycopg,
vendored by apply_flax_post). VIP-gated like the other post writers.

Every POLL_S: a slot whose Qualify passed and waits for Deploy (deploy.wants_deploy)
gets a thread running deploy.Node, at most cfg.max_parallel at once. Sole writer of
post_state.vars.deploy and post_node.vars.deployed. On start, a slice left "running"
by a previous process is closed as interrupted (its hostname file removed): the
operator's RETRY (re-run / reinstall) picks it up.
"""
import logging
import os
import threading
import time

from flax_rungs import pxeboot

from . import deploy, queries
from . import state as store

log = logging.getLogger("flax-post.deployd")
POLL_S = int(os.environ.get("FLAX_POST_DEPLOY_POLL_S", "15"))


NO_MEDIA = "no physical storage media"     # the agent's fio / smart-health skip reason


def target_for(port, row, devices) -> dict:
    kinds = {d.get("kind"): d for d in devices
             if d.get("port") == port and (not row.get("switch") or d.get("switch") == row.get("switch"))}
    b, h = kinds.get("bmc") or {}, kinds.get("host") or {}
    return {"port": port, "bmc_mac": row.get("bmc_mac"), "serial": row.get("serial"),
            "order_no": row.get("order_no"),
            "bmc_ip": b.get("lease_ip") or b.get("reservation_ip"),
            "host_ip": h.get("lease_ip") or h.get("reservation_ip"),
            "host_mac": h.get("mac"), "hostname": h.get("hostname"),
            "family": ((row.get("ladder") or {}).get("bmc_fru") or {}).get("family"),
            "run_id": (row.get("qual") or {}).get("run_id"),
            # Qualify's own finding for this run: a diskless node (deploy.has_system_storage)
            "no_media": any(((((row.get("qual") or {}).get("steps") or {}).get(s) or {})
                             .get("summary") or {}).get("reason") == NO_MEDIA
                            for s in ("fio", "smart-health"))}


def missing(t) -> "str | None":
    for k in ("bmc_mac", "serial", "bmc_ip", "host_ip", "hostname", "run_id"):
        if not t.get(k):
            return k
    return None


def close_interrupted(rows, cfg, st=store) -> None:
    for port, row in rows.items():
        dep = row.get("deploy") or {}
        if dep.get("state") != "running":
            continue
        if dep.get("hostname"):
            pxeboot.remove(dep["hostname"], pxe_dir=cfg.pxe_dir)
        dep = dict(dep, state="interrupted", verdict="fail", ended_at=int(time.time()),
                   reason="interrupted by a deployd restart; RETRY to re-run or reinstall")
        for s in (dep.get("steps") or {}).values():
            if s.get("status") in ("running", "pending"):
                s.update(status="skip", reason="interrupted")
        st.set_state(port, deploy=dep)
        log.warning("deploy on %s closed as interrupted", port)


class Runner:
    def __init__(self, cfg, creds_loader=deploy.load_creds, st=store, devices=queries.post_devices):
        self.cfg, self.creds_loader, self.st, self.devices = cfg, creds_loader, st, devices
        self.active: dict[str, threading.Thread] = {}

    def _run(self, node):
        try:
            node.run()
        finally:
            self.active.pop(node.t["port"], None)

    def once(self) -> None:
        for p in [p for p, th in self.active.items() if not th.is_alive()]:
            self.active.pop(p, None)
        rows = self.st.read_state()
        devs = None
        for port, row in sorted(rows.items()):
            if port in self.active or len(self.active) >= self.cfg.max_parallel:
                continue
            mode = deploy.wants_deploy(row)
            if mode is None:
                continue
            devs = devs if devs is not None else self.devices()
            t = target_for(port, row, devs)
            gap = missing(t)
            if gap:
                log.info("deploy on %s waits: no %s", port, gap)
                continue
            req = row.get("deploy_request") or {}
            node = deploy.Node(t, mode, store=self.st, creds=self.creds_loader(), cfg=self.cfg,
                               prior=row.get("deploy"),
                               request_at=req.get("at") if mode != "auto" else None)
            th = threading.Thread(target=self._run, args=(node,), name=f"deploy-{port}", daemon=True)
            self.active[port] = th
            log.info("deploy on %s starts (%s, run %s)", port, mode, t["run_id"])
            th.start()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = deploy.Config.from_env()
    close_interrupted(store.read_state(), cfg)
    r = Runner(cfg)
    while True:
        try:
            r.once()
        except Exception:
            log.exception("deploy pass failed")
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
