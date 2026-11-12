"""observe/solclient + observe/claims: SOL mark/read and the reconcile claim sentinel."""
from flax_post.observe import claims, solclient


def test_mark_posts_json_and_returns_body():
    seen = {}
    def transport(method, url, body, timeout):
        seen.update(method=method, url=url, body=body); return 200, '{"ok": true, "relaunched": false}'
    out = solclient.mark("10.0.0.6", "power-on", transport=transport)
    assert out == {"ok": True, "relaunched": False}
    assert seen["method"] == "POST" and seen["url"].endswith("/mark/10.0.0.6")
    assert seen["body"] == {"reason": "power-on"}


def test_mark_never_raises():
    def transport(method, url, body, timeout):
        raise OSError("down")
    out = solclient.mark("10.0.0.6", "power-on", transport=transport)
    assert out["ok"] is False and "down" in out["reason"]


def test_read_capture_returns_text_or_none(tmp_path):
    (tmp_path / "10.0.0.6.txt").write_text("=== flax-post mark x power-on ===\nPOST\n")
    assert solclient.read_capture("10.0.0.6", capture_dir=str(tmp_path)).startswith("=== flax-post")
    assert solclient.read_capture("10.0.0.7", capture_dir=str(tmp_path)) is None
    (tmp_path / "10.0.0.8.txt").write_text("")
    assert solclient.read_capture("10.0.0.8", capture_dir=str(tmp_path)) is None


def test_read_capture_keeps_the_tail_when_over_max(tmp_path):
    (tmp_path / "10.0.0.6.txt").write_text("a" * 100 + "TAIL")
    out = solclient.read_capture("10.0.0.6", capture_dir=str(tmp_path), max_bytes=10)
    assert out.endswith("TAIL") and len(out) <= 10 + len("[truncated]\n")


def test_claim_and_unclaim(tmp_path):
    d = str(tmp_path / "bmc-fw-active")
    assert claims.claim("et6b1", claim_dir=d) is True
    assert (tmp_path / "bmc-fw-active" / "et6b1").read_text().startswith("flax-post-observe")
    assert claims.unclaim("et6b1", claim_dir=d) is True
    assert not (tmp_path / "bmc-fw-active" / "et6b1").exists()
    assert claims.unclaim("et6b1", claim_dir=d) is False       # idempotent


def test_claim_over_a_foreign_file_is_refused_and_leaves_it(tmp_path):
    """/run/flax/bmc-fw-active is shared with triage's bmc_fw worker and port
    names collide across rabbit-gouda/rabbit-edam: an existing claim is never
    overwritten."""
    d = tmp_path / "bmc-fw-active"
    d.mkdir()
    (d / "et28b4").write_text("triage bmc_fw flashing et28b4\n")
    assert claims.claim("et28b4", claim_dir=str(d)) is False
    assert (d / "et28b4").read_text() == "triage bmc_fw flashing et28b4\n"


def test_unclaim_leaves_a_foreign_file_alone(tmp_path):
    d = tmp_path / "bmc-fw-active"
    d.mkdir()
    (d / "et28b4").write_text("triage bmc_fw flashing et28b4\n")
    assert claims.unclaim("et28b4", claim_dir=str(d)) is False
    assert (d / "et28b4").exists()


def test_unclaim_removes_only_our_own_marker_file(tmp_path):
    d = str(tmp_path / "bmc-fw-active")
    claims.claim("et6b1", claim_dir=d)
    # a second claim() while we hold it is an adoption: it's our own marker,
    # so it reports taken rather than refused.
    assert claims.claim("et6b1", claim_dir=d) is True
    assert claims.unclaim("et6b1", claim_dir=d) is True
    assert not (tmp_path / "bmc-fw-active" / "et6b1").exists()


def test_claim_adopts_our_own_stale_sentinel(tmp_path):
    """A flax-post-observe restart mid-boot loses the in-process claimed set
    but not the on-disk sentinel. claim() must recognize its own marker and
    report the claim taken (adopted), not refused — otherwise the port can
    never be unclaimed again."""
    d = tmp_path / "bmc-fw-active"
    d.mkdir()
    (d / "et6b1").write_text(claims.MARKER + " 2026-09-11T00:00:00Z\n")
    assert claims.claim("et6b1", claim_dir=str(d)) is True
    assert (d / "et6b1").read_text() == claims.MARKER + " 2026-09-11T00:00:00Z\n"
