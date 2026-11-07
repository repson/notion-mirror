"""Attachment store with dedupe by content hash.

Layout: ``backup/files/<sha256-12>/<name>``

- Same content → same hash → a single copy (dedupe across pages and runs).
- The name comes from the URL (the stable part); the extension is completed
  from the Content-Type when missing.
- The ``.cache/files.json`` cache maps URL identity (no query string) to the
  local file, so incremental runs do not re-download.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from pathlib import Path

from .urls import ext_from_content_type, guess_filename, url_identity

log = logging.getLogger("notion-mirror")


def sanitize_filename(name: str) -> str:
    """A reasonable, cross-platform filename."""
    name = (name or "").replace("\\", "_").replace("/", "_")
    name = re.sub(r'[\x00-\x1f<>:"|?*]', "_", name)
    name = re.sub(r"_{2,}", "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > 90:
        root, _, ext = name.partition(".")
        if ext and len(ext) <= 10:
            name = root[: 90 - len(ext) - 1] + "." + ext
        else:
            name = name[:90]
    return name or "file"


class FileStore:
    """Downloads and indexes attachments under ``<root>/files/``."""

    def __init__(self, root: Path, cache_path: Path, download_fn, link_prefix: str):
        """:param download_fn: ``fn(url, dest: Path) -> (ok: bool, content_type)``"""
        self.root = root
        self.files_dir = root / "files"
        self._cache_path = cache_path
        self._download_fn = download_fn
        self._link_prefix = link_prefix
        self._cache: dict[str, dict] = {}
        if cache_path.exists():
            try:
                self._cache = json.loads(cache_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                log.warning("Attachment cache unreadable (%s); rebuilding", cache_path)
        self._dirty = False
        self.downloaded = 0  # real downloads in this run (for the report)

    def ensure(self, url: str, sink: list | None = None) -> str | None:
        """Makes sure the URL is downloaded locally.

        Returns the path relative to the backup root (``files/<hash>/<name>``)
        or ``None`` if the download failed (a warning is logged). If ``sink``
        is given, the local path is appended to it to track the files
        referenced by the page/database being synced.
        """
        identity = url_identity(url)
        entry = self._cache.get(identity)

        if entry:
            local = entry["local"]
            if (self.root / local).exists():
                if sink is not None:
                    sink.append(local)
                return local
            # The cache points to a file that is gone: re-download.

        name = sanitize_filename(entry["name"] if entry else guess_filename(url))
        tmp_dir = self.files_dir / ".tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = tmp_dir / uuid.uuid4().hex

        try:
            ok, content_type = self._download_fn(url, tmp)
            if not ok:
                log.warning("Failed to download attachment: %s", url)
                return None

            digest = _sha256_of(tmp)[:12]
            if "." not in name:
                ext = ext_from_content_type(content_type)
                if ext:
                    name = sanitize_filename(f"{name}{ext}")

            dest_dir = self.files_dir / digest
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / name
            os.replace(tmp, dest)

            local = f"files/{digest}/{name}"
            self._cache[identity] = {"hash": digest, "name": name, "local": local}
            self._dirty = True
            self.downloaded += 1
            if sink is not None:
                sink.append(local)
            return local
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    def absolute_link(self, local: str) -> str:
        """Absolute link from the repository root (works on GitHub/Obsidian)."""
        return f"{self._link_prefix}{local}"

    def save_cache(self) -> None:
        if self._dirty:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps(self._cache, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            self._dirty = False

    def gc(self, referenced: set[str]) -> int:
        """Deletes unreferenced files (mirror semantics). Returns the count."""
        removed = 0
        if not self.files_dir.exists():
            return 0
        for entry in sorted(self.files_dir.iterdir()):
            if entry.name == ".tmp":
                for stale in entry.iterdir():
                    stale.unlink(missing_ok=True)
                continue
            if not entry.is_dir():
                continue
            for f in sorted(entry.iterdir()):
                rel = f"files/{entry.name}/{f.name}"
                if rel not in referenced:
                    f.unlink()
                    removed += 1
            if entry.is_dir() and not any(entry.iterdir()):
                entry.rmdir()
        return removed


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
