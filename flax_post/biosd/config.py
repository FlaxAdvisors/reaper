"""Runtime config for the post BIOS firmware agent (env-driven). Mirrors
flax_post.fwd.config with a POST_BIOS_FW_* namespace."""
import os

CONFIG_DIR = os.environ.get("FLAX_CONFIG_DIR", "/etc/flax")
SHARE_BASE = os.environ.get("BMC_FW_SHARE_BASE", "http://bang/export/share")
PROBE_INTERVAL_S = int(os.environ.get("POST_BIOS_FW_PROBE_S", "120"))
MODE = os.environ.get("POST_BIOS_FW_MODE", "detect")
ENABLE_PORTS = [p.strip() for p in os.environ.get("POST_BIOS_FW_ENABLE_PORTS", "").split(",") if p.strip()]
MAX_PARALLEL = int(os.environ.get("POST_BIOS_FW_MAX_PARALLEL", "24"))
