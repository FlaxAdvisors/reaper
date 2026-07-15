"""Runtime config for the post firmware driver (env-driven)."""
import os

CONFIG_DIR = os.environ.get("FLAX_CONFIG_DIR", "/etc/flax")
SHARE_BASE = os.environ.get("BMC_FW_SHARE_BASE", "http://bang/export/share")
PROBE_INTERVAL_S = int(os.environ.get("FLAX_POST_FWD_PROBE_S", "60"))
CONTROL_HOST = os.environ.get("FLAX_POST_FWD_HOST", "127.0.0.1")
CONTROL_PORT = int(os.environ.get("FLAX_POST_FWD_PORT", "8447"))

# Plan B — staged-rollout gates. Default detect = report-only (no power-on, no flash).
MODE = os.environ.get("POST_FW_MODE", "detect")
ENABLE_PORTS = [p.strip() for p in os.environ.get("POST_FW_ENABLE_PORTS", "").split(",") if p.strip()]
MAX_PARALLEL = int(os.environ.get("POST_FW_MAX_PARALLEL", "24"))
