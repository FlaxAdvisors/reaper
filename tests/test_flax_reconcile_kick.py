from flax_reconcile import kick


# --- _iface_for_vid -----------------------------------------------------------

def test_iface_for_vid_explicit_parent():
    assert kick._iface_for_vid(17, {17: "eth1"}) == "eth1.17"


def test_iface_for_vid_fallback_eth0():
    assert kick._iface_for_vid(26, {}) == "eth0.26"


# --- run_ladder ---------------------------------------------------------------

def test_ladder_bmc_ll_success_short_circuits(monkeypatch):
    monkeypatch.setattr(kick, "kick_via_bmc_ll", lambda **kw: True)
    monkeypatch.setattr(kick, "kick_via_host_ssh", lambda **kw: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(kick, "kick_via_switch_flap", lambda **kw: (_ for _ in ()).throw(AssertionError))
    rung, ok = kick.run_ladder(pool=None, switches={}, mac="aa:bb:cc:00:00:01",
                               kind="bmc", switch="s", port="p", vid=17,
                               target_ip=None, obmc_user="u", obmc_pass="p",
                               host_creds=[], flap_hold_seconds=2, reason="t",
                               vlan_parents={17: "eth0"})
    assert (rung, ok) == ("bmc_ll", True)


def test_ladder_host_ssh_short_circuits_no_flap(monkeypatch):
    flap_called = []
    monkeypatch.setattr(kick, "kick_via_bmc_ll", lambda **kw: False)
    monkeypatch.setattr(kick, "kick_via_host_ssh", lambda **kw: True)
    monkeypatch.setattr(kick, "kick_via_switch_flap",
                        lambda **kw: flap_called.append(kw) or True)
    rung, ok = kick.run_ladder(pool=None, switches={}, mac="aa:bb:cc:00:00:01",
                               kind="host", switch="s", port="p", vid=None,
                               target_ip="10.0.0.1", obmc_user="u", obmc_pass="p",
                               host_creds=[{"user": "root", "pass": "x"}],
                               flap_hold_seconds=2, reason="t")
    assert (rung, ok) == ("host_ssh", True)
    assert flap_called == []


def test_ladder_returns_none_false_when_no_switch_or_port(monkeypatch):
    monkeypatch.setattr(kick, "kick_via_bmc_ll", lambda **kw: False)
    monkeypatch.setattr(kick, "kick_via_host_ssh", lambda **kw: False)
    rung, ok = kick.run_ladder(pool=None, switches={}, mac="aa:bb:cc:00:00:01",
                               kind="bmc", switch=None, port=None, vid=17,
                               target_ip=None, obmc_user="u", obmc_pass="p",
                               host_creds=[], flap_hold_seconds=2, reason="t",
                               vlan_parents={17: "eth1"})
    assert (rung, ok) == (None, False)


def test_ladder_falls_through_to_flap(monkeypatch):
    monkeypatch.setattr(kick, "kick_via_bmc_ll", lambda **kw: False)
    monkeypatch.setattr(kick, "kick_via_host_ssh", lambda **kw: False)
    seen = {}
    def fake_flap(**kw):
        seen.update(kw); return True
    monkeypatch.setattr(kick, "kick_via_switch_flap", fake_flap)
    rung, ok = kick.run_ladder(pool="POOL", switches={"s": object()},
                               mac="aa:bb:cc:00:00:01", kind="bmc", switch="s",
                               port="p", vid=17, target_ip="172.17.0.99",
                               obmc_user="u", obmc_pass="p", host_creds=[],
                               flap_hold_seconds=2, reason="mismatch",
                               vlan_parents={17: "eth0"})
    assert (rung, ok) == ("switch_flap", True)
    assert seen["pool"] == "POOL" and seen["sw_name"] == "s"


def test_switch_flap_writes_sentinel_before_flap(monkeypatch):
    order = []
    monkeypatch.setattr("flax_reconcile.sentinel.write_sentinel",
                        lambda pool, **kw: order.append("sentinel"))
    class Sw:
        def flap(self, port, hold_seconds=None): order.append("flap")
    ok = kick.kick_via_switch_flap(pool=None, switches={"s": Sw()}, sw_name="s",
                                   port="p", kind="bmc", reason="r", mac="m",
                                   hold_seconds=2)
    assert ok is True
    assert order == ["sentinel", "flap"]  # sentinel strictly first


def test_switch_flap_sentinel_failure_does_not_block_kick(monkeypatch):
    """sentinel.write_sentinel raises → flap still runs and returns True."""
    def boom(pool, **kw):
        raise RuntimeError("db is down")
    monkeypatch.setattr("flax_reconcile.sentinel.write_sentinel", boom)
    flapped = []
    class Sw:
        def flap(self, port, hold_seconds=None): flapped.append(port)
    ok = kick.kick_via_switch_flap(pool=None, switches={"s": Sw()}, sw_name="s",
                                   port="p", kind="host", reason="r", mac="m",
                                   hold_seconds=2)
    assert ok is True
    assert flapped == ["p"]


def test_switch_flap_typeerror_fallback(monkeypatch):
    """Switch whose flap() takes no hold_seconds still flaps (TypeError fallback)."""
    monkeypatch.setattr("flax_reconcile.sentinel.write_sentinel",
                        lambda pool, **kw: None)
    flapped = []
    class LegacySw:
        def flap(self, port): flapped.append(port)  # no hold_seconds kwarg
    ok = kick.kick_via_switch_flap(pool=None, switches={"s": LegacySw()}, sw_name="s",
                                   port="eth1", kind="bmc", reason="r", mac="m",
                                   hold_seconds=2)
    assert ok is True
    assert flapped == ["eth1"]


def test_switch_flap_marks_pending_before_flap_clears_after(monkeypatch):
    """flap-pending marker is written BEFORE flap() (so a process death mid-flap
    is recoverable) and cleared AFTER a successful flap."""
    order = []
    monkeypatch.setattr("flax_reconcile.sentinel.write_sentinel",
                        lambda pool, **kw: None)
    monkeypatch.setattr(kick.db, "mark_flap_pending",
                        lambda pool, **kw: order.append("mark"))
    monkeypatch.setattr(kick.db, "clear_flap_pending",
                        lambda pool, **kw: order.append("clear"))
    class Sw:
        def flap(self, port, hold_seconds=None): order.append("flap")
    ok = kick.kick_via_switch_flap(pool="POOL", switches={"s": Sw()}, sw_name="s",
                                   port="Ethernet8/3", kind="host", reason="r",
                                   mac="m", hold_seconds=2)
    assert ok is True
    assert order == ["mark", "flap", "clear"]


def test_switch_flap_failure_leaves_marker(monkeypatch):
    """A flap that raises does NOT clear the marker (so startup self-heal can
    re-issue the no-shutdown if the shutdown landed)."""
    order = []
    monkeypatch.setattr("flax_reconcile.sentinel.write_sentinel",
                        lambda pool, **kw: None)
    monkeypatch.setattr(kick.db, "mark_flap_pending",
                        lambda pool, **kw: order.append("mark"))
    monkeypatch.setattr(kick.db, "clear_flap_pending",
                        lambda pool, **kw: order.append("clear"))
    class Sw:
        def flap(self, port, hold_seconds=None):
            raise RuntimeError("switch unreachable on no-shutdown")
    ok = kick.kick_via_switch_flap(pool="POOL", switches={"s": Sw()}, sw_name="s",
                                   port="Ethernet8/3", kind="host", reason="r",
                                   mac="m", hold_seconds=2)
    assert ok is False
    assert "mark" in order
    assert "clear" not in order


def test_eui64_ll_known_vector():
    from flax_reconcile.kick import _eui64_ll_from_mac
    # 00:25:90:ab:cd:ef -> flip U/L bit, insert ff:fe
    assert _eui64_ll_from_mac("00:25:90:ab:cd:ef") == "fe80::225:90ff:feab:cdef"


# --- kick_via_bmc_ll: real DHCP release, not a bare `ip link` toggle ----------

def _capture_bmc_ll_cmd(monkeypatch):
    """Drive kick_via_bmc_ll with a fake ssh_runner and return the remote cmd."""
    sent = {}

    def fake_runner(host, user, password, cmd):
        sent["host"] = host
        sent["cmd"] = cmd
        return ""

    ok = kick.kick_via_bmc_ll(
        mac="98:03:9b:a6:fd:ac", iface="eth1.25",
        obmc_user="root", obmc_pass="x",
        ssh_runner=fake_runner, ping_runner=lambda addr, timeout: True)
    sent["ok"] = ok
    return sent


def test_bmc_ll_kick_drops_lease_and_rediscovers(monkeypatch):
    """A Phosphor/systemd-networkd BMC won't re-DHCP on a bare `ip link` toggle
    (it just renews the held pool lease). The bmc_ll rung must drop the cached
    networkd lease and re-run DHCP so the device lands on its reservation."""
    sent = _capture_bmc_ll_cmd(monkeypatch)
    assert sent["ok"] is True
    cmd = sent["cmd"]
    # Drops the cached systemd-networkd lease so the next DHCP is a DISCOVER,
    # not a renew of the stale pool address.
    assert "/run/systemd/netif/leases/" in cmd
    # Triggers a real re-DHCP via networkd, not only a link toggle.
    assert ("networkctl reconfigure" in cmd) or (
        "systemctl restart systemd-networkd" in cmd)


def test_bmc_ll_kick_still_backgrounds_so_bounce_survives(monkeypatch):
    """Bouncing the BMC's mgmt iface kills the SSH session, so the kick must be
    detached (nohup &) or the runner sees a broken pipe and reports failure."""
    sent = _capture_bmc_ll_cmd(monkeypatch)
    assert "nohup" in sent["cmd"] and sent["cmd"].rstrip().endswith("&")


def test_bmc_ll_kick_targets_link_local_with_iface_scope(monkeypatch):
    """The runner is invoked against the EUI-64 LL scoped to the host sub-iface."""
    sent = _capture_bmc_ll_cmd(monkeypatch)
    # 98:03:9b:a6:fd:ac -> flip U/L bit on first octet -> 9a03:9bff:fea6:fdac
    assert sent["host"] == "fe80::9a03:9bff:fea6:fdac%eth1.25"


def test_default_ssh_runner_password_in_env_not_argv(monkeypatch):
    seen = {}

    def fake(argv, **kw):
        seen.update(argv=argv, **kw)
        return ""
    monkeypatch.setattr(kick.subprocess, "check_output", fake)
    kick._default_ssh_runner("fe80::1%eth0.17", "root", "fake-obmc-pw", "true")
    assert seen["argv"][:2] == ["sshpass", "-e"]
    assert not any("fake-obmc-pw" in a for a in seen["argv"])
    assert seen["env"]["SSHPASS"] == "fake-obmc-pw"
