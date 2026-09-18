import asyncio

import pytest

from dct_hub.blobs import BlobMissing, LocalBlobs, artifact_locator


def run(coro):
    return asyncio.run(coro)


def stage(blobs, content: bytes):
    tmp = blobs.staging_path("ignored")
    tmp.write_bytes(content)
    return tmp


def read_all(blobs, locator) -> bytes:
    async def collect():
        return b"".join([chunk async for chunk in blobs.read(locator)])

    return run(collect())


def test_round_trip(tmp_path):
    blobs = LocalBlobs(tmp_path / "artifacts")
    run(blobs.put_file("b/k.html", stage(blobs, b"<html>v1</html>")))
    assert run(blobs.exists("b/k.html")) is True
    assert read_all(blobs, "b/k.html") == b"<html>v1</html>"


def test_publish_is_atomic_replace(tmp_path):
    blobs = LocalBlobs(tmp_path / "artifacts")
    run(blobs.put_file("b/k.html", stage(blobs, b"v1")))
    run(blobs.put_file("b/k.html", stage(blobs, b"v2")))
    assert read_all(blobs, "b/k.html") == b"v2"
    # no staging residue alongside the published blob
    assert [p.name for p in (tmp_path / "artifacts" / "b").iterdir()] == ["k.html"]
    assert list(blobs.staging_dir.iterdir()) == []


def test_read_missing_raises(tmp_path):
    blobs = LocalBlobs(tmp_path / "artifacts")
    assert run(blobs.exists("nope.html")) is False
    with pytest.raises(BlobMissing):
        read_all(blobs, "nope.html")


def test_delete(tmp_path):
    blobs = LocalBlobs(tmp_path / "artifacts")
    run(blobs.put_file("b/k.html", stage(blobs, b"x")))
    run(blobs.delete("b/k.html"))
    assert run(blobs.exists("b/k.html")) is False
    run(blobs.delete("b/k.html"))  # idempotent


def test_locator_format():
    assert artifact_locator("finance/q4", "abc123", "html") == "finance/q4/abc123.html"
    with pytest.raises(ValueError):
        artifact_locator("b", "k", "ht ml")
