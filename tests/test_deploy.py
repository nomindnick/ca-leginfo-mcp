"""Unit tests for the deploy layer's pure logic (network calls are
exercised only in the live deployment, not here)."""

from pathlib import Path

import pytest

from deploy import nightly, r2sync
from server.ratelimit import RateLimitMiddleware

# --- nightly helpers -----------------------------------------------------

def test_newest_law_zip_from_index():
    html = ('<a href="pubinfo_2023.zip">x</a> <a href="pubinfo_2025.zip">'
            '</a> <a href="pubinfo_daily_Mon.zip"></a>'
            '<a href="pubinfo_1989.zip"></a>')
    assert nightly._newest_law_zip(html) == "pubinfo_2025.zip"


def test_newest_law_zip_empty_index_raises():
    with pytest.raises(RuntimeError):
        nightly._newest_law_zip("<html>maintenance</html>")


def test_last_modified_parsing():
    import email.utils

    hdr = "Wed, 05 Aug 2026 04:23:11 GMT"
    ts = nightly._last_modified({"Last-Modified": hdr})
    assert email.utils.formatdate(ts, usegmt=True) == hdr
    assert nightly._last_modified({}) is None


# --- r2sync etag sidecar -------------------------------------------------

class _FakeClientError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class _FakeS3:
    class exceptions:
        ClientError = _FakeClientError

    def __init__(self, etag="\"abc\""):
        self.etag = etag
        self.downloads = 0

    def head_object(self, Bucket, Key):
        return {"ETag": self.etag}

    def download_file(self, bucket, key, dest):
        self.downloads += 1
        Path(dest).write_bytes(b"data-" + self.etag.encode())


def test_download_skips_when_etag_matches(tmp_path, monkeypatch):
    monkeypatch.setenv("R2_BUCKET", "test")
    s3 = _FakeS3()
    dest = tmp_path / "current.db"
    assert r2sync.download(s3, "current.db", dest) is True
    assert s3.downloads == 1
    # Same etag: skipped.
    assert r2sync.download(s3, "current.db", dest) is False
    assert s3.downloads == 1
    # New remote etag: re-downloaded.
    s3.etag = "\"def\""
    assert r2sync.download(s3, "current.db", dest) is True
    assert s3.downloads == 2
    # only_if_changed=False forces the transfer.
    assert r2sync.download(s3, "current.db", dest,
                           only_if_changed=False) is True
    assert s3.downloads == 3


def test_download_missing_object_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("R2_BUCKET", "test")

    class _Missing(_FakeS3):
        def head_object(self, Bucket, Key):
            raise _FakeClientError("404")

    with pytest.raises(FileNotFoundError):
        r2sync.download(_Missing(), "nope.db", tmp_path / "nope.db")


# --- rate limiter --------------------------------------------------------

def test_rate_limit_window():
    rl = RateLimitMiddleware(None, per_minute=3)
    t = 1000.0
    assert all(rl.allow("a", t + i) for i in range(3))
    assert rl.allow("a", t + 3) is False       # 4th within the window
    assert rl.allow("b", t + 3) is True        # other clients unaffected
    assert rl.allow("a", t + 61.5) is True     # window slid


def test_rate_limit_client_key_from_forwarded_header():
    rl = RateLimitMiddleware(None)
    scope = {"headers": [(b"x-forwarded-for", b"1.2.3.4, 10.0.0.1")],
             "client": ("10.0.0.9", 1234)}
    assert rl._client(scope) == "1.2.3.4"
    assert rl._client({"headers": [], "client": ("10.0.0.9", 1)}) == "10.0.0.9"
