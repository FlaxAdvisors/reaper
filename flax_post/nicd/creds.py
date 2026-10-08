"""Host SSH creds for nicd (reuse the biosd loader). nicd never talks to the
BMC: a NIC flash or UEFI change needs only the card reset."""
from flax_post.biosd.creds import load_host_creds  # noqa: F401  (re-export)
