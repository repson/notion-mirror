"""URL utilities: tell Notion-hosted files apart from page links."""

from __future__ import annotations

import mimetypes
from urllib.parse import unquote, urlsplit

# Hosts serving files uploaded to Notion (the signed URLs expire, but the
# path is stable and serves as the file identity).
_INTERNAL_FILE_HOSTS = (
    "prod-files-secure.s3.us-west-2.amazonaws.com",
    "files.notion.so",
    "file.notion.so",
)

# Notion domain suffixes (pages, image proxy, published sites).
_NOTION_DOMAINS = (
    "notion.so",
    "notion.com",
    "notion.site",
    "notion-static.com",
)

_EXT_BY_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/bmp": ".bmp",
    "application/pdf": ".pdf",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/wav": ".wav",
    "audio/ogg": ".ogg",
    "application/zip": ".zip",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}


def is_notion_file_url(url: str) -> bool:
    """True if the URL points to a file hosted by Notion (downloadable).

    Tells files apart from page links: ``https://www.notion.so/My-page-abc`` is
    a link and must NOT be downloaded; ``.../image/...`` or the signed S3
    URLs are files.
    """
    if not url.startswith(("http://", "https://")):
        return False
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path.lower()

    if any(host == h or host.endswith("." + h) for h in _INTERNAL_FILE_HOSTS):
        return True

    # Legacy format: https://s3-us-west-2.amazonaws.com/secure.notion-static.com/...
    if host.endswith("amazonaws.com"):
        return "notion-static" in path or "prod-files" in path

    if any(host == d or host.endswith("." + d) for d in ("notion.so", "notion.com")):
        # Notion image/icon proxy.
        return path.startswith("/image/") or path.startswith("/signed/")

    return False


def is_http_url(url: str) -> bool:
    return isinstance(url, str) and url.startswith(("http://", "https://"))


def url_identity(url: str) -> str:
    """URL without query or fragment: Notion URLs are signed and the
    signature changes on every response; the path is the stable identity."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def guess_filename(url: str) -> str:
    """A reasonable filename from the URL (no query string)."""
    path = unquote(urlsplit(url).path)
    name = path.rsplit("/", 1)[-1] if "/" in path else path
    # URLs like /image/<encoded url>: decode once more.
    if "%" in name:
        name = unquote(name)
        name = name.rsplit("/", 1)[-1] if "/" in name else name
    name = name.strip()
    return name or "file"


def ext_from_content_type(content_type: str | None) -> str:
    """Extension for a given Content-Type (with basic normalization)."""
    if not content_type:
        return ""
    ct = content_type.split(";")[0].strip().lower()
    if ct in _EXT_BY_CONTENT_TYPE:
        return _EXT_BY_CONTENT_TYPE[ct]
    guessed = mimetypes.guess_extension(ct) or ""
    # mimetypes returns .jpe for jpeg on some systems.
    return ".jpg" if guessed == ".jpe" else guessed
