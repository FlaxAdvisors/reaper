# flax_post/observe/claims.py
"""The reconcile claim sentinel (flax_reconcile/claims.py reads it): while a
slot's boot window is open, /run/flax/bmc-fw-active/<port> tells reconcile not
to kick/flap the port (power-on drops the host link ~8s plus a mid-boot flap;
recon note 2026-06-22). Internal et6b1 port form, one flat file per port.

OWNERSHIP. This directory is NOT ours: triage's bmc_fw worker writes the same
flat files for the ports it is flashing, and the bare port name carries no
switch qualifier — `et28b4` on rabbit-gouda (triage) and on rabbit-edam (post)
are the same filename. Post and reconcile are VIP-gated to the same master
bang and reconcile mounts /run/flax, so the namespace is genuinely shared.
Hence: claim() never clobbers an existing file (O_CREAT|O_EXCL, returns False
when someone else holds it) and unclaim() removes only a file that starts with
our MARKER. Deleting a foreign claim mid-flash would pull reconcile's interlock
out from under triage. Recorded as entry 9 in docs/flax-storage-delta.md.
"""
import logging
import os
import time

log = logging.getLogger("flax-post.claims")
CLAIM_DIR = os.environ.get("FLAX_POST_CLAIM_DIR", "/run/flax/bmc-fw-active")
# First bytes of every file claim() writes; the ownership test for unclaim().
MARKER = "flax-post-observe slot ladder"


def claim(port, claim_dir=None) -> bool:
    """Take the sentinel for `port`. True when THIS writer created it, or
    when the file that already exists is OUR marker (adopted after a
    restart: the on-disk sentinel survives a `flax-post-observe` restart
    even though the in-process claimed-set does not). False when the file
    exists and is foreign — left untouched, never overwritten — or the
    write failed."""
    d = claim_dir or CLAIM_DIR
    path = os.path.join(d, port)
    try:
        os.makedirs(d, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        try:
            with open(path) as f:
                head = f.read(len(MARKER))
        except OSError:
            log.exception("claim read failed for %s", port)
            return False
        if head.startswith(MARKER):
            log.debug("claim %s: adopted existing claim (ours from an earlier run)", port)
            return True
        log.info("claim %s already held (foreign); not taken", port)
        return False
    except OSError:
        log.exception("claim write failed for %s", port)
        return False
    try:
        with os.fdopen(fd, "w") as f:
            f.write("%s %s\n" % (MARKER, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
    except OSError:
        log.exception("claim write failed for %s", port)
        return False
    return True


def unclaim(port, claim_dir=None) -> bool:
    """Drop OUR sentinel for `port`. A file we did not write (no MARKER) is
    left in place and logged at debug. True when a file of ours was removed."""
    path = os.path.join(claim_dir or CLAIM_DIR, port)
    try:
        with open(path) as f:
            head = f.read(len(MARKER))
    except FileNotFoundError:
        return False
    except OSError:
        log.exception("claim read failed for %s", port)
        return False
    if not head.startswith(MARKER):
        log.debug("claim %s is not ours (%r); left in place", port, head)
        return False
    try:
        os.remove(path)
    except FileNotFoundError:
        return False
    except OSError:
        log.exception("claim remove failed for %s", port)
        return False
    return True
