"""rackpost's build id: `<flax_post version>·<git short sha>[-dirty-<hash>]`.

The apply_rackpost role computes it on the controller from the checkout it deploys
and writes it to <deploy dir>/BUILD (beside flax_rack/); read once at daemon start.
Without that file (a dev checkout): `<flax_post version>·dev`.
"""
import os
from pathlib import Path

from flax_post.version import __version__

BUILD_FILE = Path(os.environ.get("RACKPOST_BUILD_FILE",
                                 Path(__file__).resolve().parent.parent / "BUILD"))


def _read_build() -> str:
    try:
        return BUILD_FILE.read_text().strip() or f"{__version__}·dev"
    except OSError:
        return f"{__version__}·dev"


BUILD = _read_build()
