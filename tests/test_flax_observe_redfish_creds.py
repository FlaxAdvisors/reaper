"""Redfish credential wiring for observe's BMC identification.

The braintree TiogaPass AMI BMCs authenticate only with the Redfish cred set in
/etc/flax/credentials-redfish.json (rfuser=Administrator), NOT the IPMI/openbmc
USERID set in credentials-bmc.json. The loader must read the rfuser/rfpass
schema, and probe_bmc_kind must feed the REDFISH creds (not bmc_creds) to its
Redfish product-name read.
"""


def test_load_redfish_credentials_reads_rfuser_rfpass(tmp_path):
    from flax_observe.ipmi import _load_redfish_credentials
    p = tmp_path / "credentials-redfish.json"
    p.write_text('[{"rfuser":"Administrator","rfpass":"secret"},'
                 '{"rfuser":"root","rfpass":"x"}]')
    creds = _load_redfish_credentials(str(p))
    # normalized to the {bmcuser,bmcpass} shape every consumer already uses
    assert creds == [{"bmcuser": "Administrator", "bmcpass": "secret"},
                     {"bmcuser": "root", "bmcpass": "x"}]


def test_load_redfish_credentials_back_compat_bmcuser(tmp_path):
    """A file already in the {bmcuser,bmcpass} schema still loads."""
    from flax_observe.ipmi import _load_redfish_credentials
    p = tmp_path / "credentials-redfish.json"
    p.write_text('[{"bmcuser":"USERID","bmcpass":"y"}]')
    assert _load_redfish_credentials(str(p)) == [{"bmcuser": "USERID",
                                                  "bmcpass": "y"}]


def test_load_redfish_credentials_missing_file_is_empty():
    from flax_observe.ipmi import _load_redfish_credentials
    assert _load_redfish_credentials("/nonexistent/credentials-redfish.json") == []


def test_probe_bmc_kind_redfish_branch_uses_redfish_creds():
    """probe_bmc_kind must feed the REDFISH creds to the redfish probe, not the
    (USERID) bmc_creds that these AMI boards reject with 401."""
    from flax_observe.bmc_probe import probe_bmc_kind
    seen = {}

    def fake_redfish(ip, creds):
        seen["creds"] = creds
        return {"is_bmc": True, "product_name": "Wiwynn"}

    r = probe_bmc_kind(
        ip="172.17.8.102",
        credentials={"obmcuser": "root", "obmcpass": "x"},
        bmc_creds=[{"bmcuser": "USERID", "bmcpass": "bad"}],
        redfish_creds=[{"bmcuser": "Administrator", "bmcpass": "good"}],
        port_probe=lambda h: {"ssh": False, "ipmi": False, "redfish": True},
        redfish_probe=fake_redfish,
    )
    assert r["kind"] == "redfish"
    assert r["product_name"] == "Wiwynn"
    assert seen["creds"] == [{"bmcuser": "Administrator", "bmcpass": "good"}]
