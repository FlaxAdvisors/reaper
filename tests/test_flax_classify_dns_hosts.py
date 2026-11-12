"""Tests for flax_classify.dns_hosts — dnsmasq device-hosts renderer + writer."""


def test_render_hosts_emits_a_and_aaaa_sorted():
    from flax_classify.dns_hosts import render_hosts
    recs = [
        {"hostname": "leopard7b1", "ipv4": "172.16.7.1", "ipv6": "fd00:16::7:1"},
        {"hostname": "leopard7b1-bmc", "ipv4": "172.16.7.101", "ipv6": "fd00:16::7:101"},
    ]
    out = render_hosts(recs)
    lines = out.splitlines()
    assert "172.16.7.1\tleopard7b1" in lines
    assert "fd00:16::7:1\tleopard7b1" in lines
    assert "172.16.7.101\tleopard7b1-bmc" in lines
    assert "fd00:16::7:101\tleopard7b1-bmc" in lines
    # deterministic ordering (sorted) so unchanged input -> identical file
    assert out == render_hosts(list(reversed(recs)))


def test_render_hosts_skips_missing_fields():
    from flax_classify.dns_hosts import render_hosts
    out = render_hosts([{"hostname": "h", "ipv4": "1.2.3.4", "ipv6": None}])
    assert "1.2.3.4\th" in out
    assert "h" in out and "None" not in out


def test_write_hosts_file_atomic_and_idempotent(tmp_path):
    from flax_classify.dns_hosts import write_hosts_file
    p = tmp_path / "flax-devices"
    recs = [{"hostname": "h", "ipv4": "1.2.3.4", "ipv6": "fd00:1::4"}]
    changed1 = write_hosts_file(str(p), recs)
    assert changed1 is True and p.read_text()
    # second identical write -> no change reported, no rewrite churn
    changed2 = write_hosts_file(str(p), recs)
    assert changed2 is False
    # no leftover temp file
    assert not list(tmp_path.glob("*.tmp"))


def test_render_hosts_appends_aliases_as_extra_names():
    """user_context.aliases become additional space-separated names on each
    A/AAAA line (dnsmasq hostsdir supports multiple names per IP)."""
    from flax_classify.dns_hosts import render_hosts
    recs = [{"hostname": "leopard7b1", "ipv4": "172.16.7.1",
             "ipv6": "fd00:16::7:1", "aliases": ["leo-mgmt", "extra1"]}]
    lines = render_hosts(recs).splitlines()
    assert "172.16.7.1\tleopard7b1 leo-mgmt extra1" in lines
    assert "fd00:16::7:1\tleopard7b1 leo-mgmt extra1" in lines


def test_render_hosts_empty_or_missing_aliases_unchanged():
    from flax_classify.dns_hosts import render_hosts
    base = render_hosts([{"hostname": "h", "ipv4": "1.2.3.4", "ipv6": None}])
    empty = render_hosts([{"hostname": "h", "ipv4": "1.2.3.4", "ipv6": None,
                           "aliases": []}])
    assert base == empty
    assert "1.2.3.4\th" in base.splitlines()
