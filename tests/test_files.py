import json
from pathlib import Path

from notion_mirror.files import FileStore, sanitize_filename


def make_store(tmp_path: Path):
    downloads = []

    def download_fn(url: str, dest):
        downloads.append(url)
        Path(dest).write_bytes(b"content:" + url.encode())
        content_type = "image/png" if ".png" in url else "application/pdf"
        return True, content_type

    store = FileStore(tmp_path, tmp_path / ".cache" / "files.json", download_fn, "/backup/")
    return store, downloads


def test_sanitize_filename():
    assert sanitize_filename('bad:"name"/file?.pdf') == "bad_name_file_.pdf"
    assert sanitize_filename("") == "file"
    assert sanitize_filename("  . hidden . ") == "hidden"


def test_ensure_downloads_and_dedupes(tmp_path):
    store, downloads = make_store(tmp_path)
    url = "https://prod-files-secure.s3.us-west-2.amazonaws.com/WS/IMG/logo.png?sig=1"

    local = store.ensure(url)
    assert local.startswith("files/")
    assert (tmp_path / local).exists()
    assert local.endswith("logo.png")
    assert len(downloads) == 1

    # Second call (same URL, different signature): no re-download.
    assert store.ensure(
        "https://prod-files-secure.s3.us-west-2.amazonaws.com/WS/IMG/logo.png?sig=OTHER"
    ) == local
    assert len(downloads) == 1


def test_ensure_sink_accumulates_references(tmp_path):
    store, _ = make_store(tmp_path)
    sink: list = []
    url = "https://prod-files-secure.s3.us-west-2.amazonaws.com/WS/DOC/doc.pdf?sig=1"
    local = store.ensure(url, sink)
    assert sink == [local]
    # Extension from Content-Type when the URL has none.
    local2 = store.ensure("https://prod-files-secure.s3.us-west-2.amazonaws.com/WS/DOC/2?sig=1", sink)
    assert local2.endswith(".pdf")


def test_ensure_failed_download(tmp_path):
    def fail_fn(url, dest):
        return False, None

    store = FileStore(tmp_path, tmp_path / ".cache" / "files.json", fail_fn, "/backup/")
    assert store.ensure("https://prod-files-secure.s3.us-west-2.amazonaws.com/WS/IMG/x.png") is None


def test_gc_removes_unreferenced(tmp_path):
    store, _ = make_store(tmp_path)
    keep = store.ensure("https://prod-files-secure.s3.us-west-2.amazonaws.com/WS/A/a.png?sig=1")
    drop = store.ensure("https://prod-files-secure.s3.us-west-2.amazonaws.com/WS/B/b.png?sig=1")
    assert (tmp_path / drop).exists()

    removed = store.gc({keep})
    assert removed == 1
    assert (tmp_path / drop).exists() is False
    assert (tmp_path / keep).exists()


def test_cache_persisted_and_reloaded(tmp_path):
    store, _ = make_store(tmp_path)
    url = "https://prod-files-secure.s3.us-west-2.amazonaws.com/WS/IMG/logo.png?sig=1"
    local = store.ensure(url)
    store.save_cache()

    cache = json.loads((tmp_path / ".cache" / "files.json").read_text(encoding="utf-8"))
    assert cache[url_identity_of(url)]["local"] == local


def url_identity_of(url: str) -> str:
    from notion_mirror.urls import url_identity

    return url_identity(url)
