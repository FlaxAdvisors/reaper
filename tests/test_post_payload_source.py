import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]

POST = (REPO / "roles/apply_pxe_payloads/files/post")


def test_post_source_is_text_only_no_binaries():
    assert POST.is_dir()
    for p in POST.iterdir():
        if p.is_dir() or p.name.endswith(".md"):
            continue
        data = p.read_bytes()
        assert b"\x00" not in data, "binary blob committed: %s" % p.name    # no NUL bytes


def test_no_committed_tarball_or_scelnx():
    assert not (POST / "post.tgz").exists()
    assert not (POST / "SCELNX_64").exists()          # binary -> assembled at build, not git


