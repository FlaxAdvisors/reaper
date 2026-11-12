import importlib, json

def test_config_defaults(monkeypatch):
    for k in ("POST_NIC_FW_MODE", "POST_NIC_FW_ENABLE_PORTS", "POST_NIC_FW_MAX_PARALLEL",
              "POST_NIC_FW_PROBE_S", "POST_NIC_RESET_TIMEOUT",
              "POST_NIC_FLASH_TIMEOUT"):
        monkeypatch.delenv(k, raising=False)
    import flax_post.nicd.config as c
    importlib.reload(c)
    assert c.MODE == "detect" and c.ENABLE_PORTS == [] and c.MAX_PARALLEL == 24
    assert c.PROBE_INTERVAL_S == 120 and c.RESET_TIMEOUT == 120
    assert c.FLASH_TIMEOUT == 900

def test_config_enable_ports_parsed(monkeypatch):
    monkeypatch.setenv("POST_NIC_FW_ENABLE_PORTS", "et10b1, et10b2 ")
    import flax_post.nicd.config as c
    importlib.reload(c)
    assert c.ENABLE_PORTS == ["et10b1", "et10b2"]

