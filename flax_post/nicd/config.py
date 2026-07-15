"""Runtime config for the post NIC firmware agent (POST_NIC_FW_* namespace)."""
import os

CONFIG_DIR = os.environ.get("FLAX_CONFIG_DIR", "/etc/flax")
SHARE_BASE = os.environ.get("BMC_FW_SHARE_BASE", "http://bang/export/share")
PROBE_INTERVAL_S = int(os.environ.get("POST_NIC_FW_PROBE_S", "120"))
MODE = os.environ.get("POST_NIC_FW_MODE", "detect")
ENABLE_PORTS = [p.strip() for p in os.environ.get("POST_NIC_FW_ENABLE_PORTS", "").split(",") if p.strip()]
MAX_PARALLEL = int(os.environ.get("POST_NIC_FW_MAX_PARALLEL", "24"))
RESET_TIMEOUT = int(os.environ.get("POST_NIC_RESET_TIMEOUT", "120"))
BMC_RESET_TIMEOUT = int(os.environ.get("POST_NIC_BMC_RESET_TIMEOUT", "300"))
FLASH_TIMEOUT = int(os.environ.get("POST_NIC_FLASH_TIMEOUT", "900"))
# Test knob: force the verified BMC cold-reset after a flash even when no UEFI
# bit was toggled, to exercise the mc-reset-cold / ssh-reboot path on HW.
FORCE_BMC_RESET = os.environ.get("POST_NIC_FW_FORCE_BMC_RESET", "").lower() in ("1", "true", "yes")
