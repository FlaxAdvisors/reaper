# flax_post/observe/claims.py
"""The reconcile claim sentinel (flax_reconcile/claims.py reads it): while a
slot's boot window is open, /run/flax/bmc-fw-active/<port> tells reconcile not
to kick/flap the port (power-on drops the host link ~8s plus a mid-boot flap;
recon note 2026-06-22). Internal et6b1 port form, one flat file per port."""
import logging
import os
import time

log = logging.getLogger("flax-post.claims")
CLAIM_DIR = os.environ.get("FLAX_POST_CLAIM_DIR", "/run/flax/bmc-fw-active")


def claim(port, claim_dir=None) -> None:
    d = claim_dir or CLAIM_DIR
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, port), "w") as f:
            f.write("flax-post-observe slot ladder %s\n" % time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    except OSError:
        log.exception("claim write failed for %s", port)


def unclaim(port, claim_dir=None) -> None:
    try:
        os.remove(os.path.join(claim_dir or CLAIM_DIR, port))
    except FileNotFoundError:
        pass
    except OSError:
        log.exception("claim remove failed for %s", port)
