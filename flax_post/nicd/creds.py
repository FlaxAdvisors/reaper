"""Host SSH creds for nicd (reuse the biosd loader). The BMC is reached over
Redfish (not SSH) -- its creds come from flax_post.fwd.creds.load_redfish_creds
(credentials-bmc.json), used directly in __main__ for the Manager.Reset."""
from flax_post.biosd.creds import load_host_creds  # noqa: F401  (re-export)
