"""flax_post.fwd.artifact: HTTP fetch of the .mtd.tar image."""
import io
import pytest


def test_fetch_builds_url_and_returns_bytes(monkeypatch):
    seen = {}

    class _Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(url, timeout=None):
        seen["url"] = url; seen["timeout"] = timeout
        return _Resp(b"FIRMWARE")

    from flax_post.fwd import artifact
    monkeypatch.setattr(artifact.urllib.request, "urlopen", fake_urlopen)
    data = artifact.fetch("http://bang/export/share/", "OpenBMC/fx-tp.mtd.tar", timeout=30)
    assert data == b"FIRMWARE"
    assert seen["url"] == "http://bang/export/share/OpenBMC/fx-tp.mtd.tar"  # single slash join
    assert seen["timeout"] == 30


def test_fetch_error_raises_runtimeerror(monkeypatch):
    def boom(url, timeout=None):
        raise OSError("connection refused")
    from flax_post.fwd import artifact
    monkeypatch.setattr(artifact.urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError):
        artifact.fetch("http://bang/export/share", "x.mtd.tar")
