# family_map_live.py -- the deployed family map, reloaded when it changes.
#
# COPIED VERBATIM between flax_observe/ and flax_post/observe/ (reaper-devel
# tests/test_fru_copies_drift.py). Reload rule = flax_discover.cycle
# _maybe_reload_family_map: the newest mtime across the directory and its
# *.txt files. The directory is a bind mount (not a single file), so an edit
# on the host is seen without a restart.
import os
import re
import threading

from .family_map import load_family_map_dir

FAMILY_MAP_DIR = os.environ.get("FLAX_FAMILY_MAP_DIR", "/etc/flax/family-map")

_lock = threading.Lock()
_cache = {"path": None, "mtime": None, "map": {}}


def _dir_mtime(path):
    try:
        mtimes = [os.path.getmtime(path)]
        names = os.listdir(path)
    except OSError:
        return None
    for name in names:
        if name.endswith(".txt"):
            try:
                mtimes.append(os.path.getmtime(os.path.join(path, name)))
            except OSError:
                continue
    return max(mtimes)


def current(path=None):
    """The family map at `path` (default FAMILY_MAP_DIR). A missing directory
    is {}: every family is unknown and the Product Serial is used. A file that
    fails to load (bad regex) keeps the previous map until it is fixed."""
    path = path or FAMILY_MAP_DIR
    mtime = _dir_mtime(path)
    with _lock:
        if _cache["path"] != path or _cache["mtime"] != mtime:
            if mtime is None:
                fm = {}
            else:
                try:
                    fm = load_family_map_dir(path)
                except (OSError, re.error):
                    fm = _cache["map"] if _cache["path"] == path else {}
            _cache.update(path=path, mtime=mtime, map=fm)
        return _cache["map"]
