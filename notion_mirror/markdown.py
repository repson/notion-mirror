"""Link rewriting and post-processing for the Markdown exported by Notion.

Notion's internal file URLs are signed and expire in ~1h, so the backup
replaces them with local paths (``/backup/files/<hash>/<name>``) so that
attachments render when browsing the repo on GitHub, Obsidian, VS Code...

Root-relative paths (starting with ``/``) are used instead of relative ones
because they do not depend on the page's depth: when a page moves, its
Markdown does not change and does not need to be re-downloaded.

Post-processing fixes Notion-specific markup that doesn't render outside of
Notion: missing page titles, ``<page>``/``<database>`` tags, ``<callout>``
blocks, and ``<table-of-contents>`` placeholders.
"""

from __future__ import annotations

import re

# Markdown links and images: ![alt](url "title") or [text](url "title").
_LINK_RE = re.compile(r'(!?)\[([^\]\[]*)\]\(([^)\s]+)(?:\s+"([^"]*)")?\)')

# Links pointing to a local backup attachment: .../files/<hash>/<name>.
_LOCAL_FILE_RE = re.compile(r"\(([^)\s]*/files/[0-9a-fA-F]{8,}/[^)/\s]+)\)")

# Notion-specific tags that need conversion for standard Markdown viewers.
# <page url="https://app.notion.com/p/32HEXCHARS">Title</page>
_PAGE_TAG_RE = re.compile(
    r'<page\s+url="https://app\.notion\.com/p/([a-f0-9]{32})"[^>]*>\s*(.*?)\s*</page>',
    re.DOTALL,
)

# <database url="..." data-source-url="collection://UUID" ...>Title</database>
_DATABASE_TAG_RE = re.compile(
    r'<database\s+[^>]*data-source-url="collection://([a-f0-9-]{36})"[^>]*>\s*(.*?)\s*</database>',
    re.DOTALL,
)

# <callout icon="..." color="...">content</callout>
_CALLOUT_RE = re.compile(
    r'<callout\s+icon="[^"]*"\s+color="[^"]*">(.*?)</callout>',
    re.DOTALL,
)

# <table-of-contents />
_TOC_RE = re.compile(r'<table-of-contents\s*/>')


def extract_urls(markdown: str) -> list[str]:
    """All URLs appearing as a link or image in the Markdown."""
    return [m.group(3) for m in _LINK_RE.finditer(markdown or "")]


def rewrite_links(markdown: str, mapper) -> tuple[str, int]:
    """Rewrites link/image URLs with ``mapper(url) -> str | None``.

    - If the mapper returns a new URL, the link is rebuilt with it
      (preserving alt/title).
    - If it returns ``None`` or the same URL, the link is left untouched.
    Returns ``(new_markdown, number_of_rewritten_links)``.
    """
    count = 0

    def _sub(match: re.Match) -> str:
        nonlocal count
        bang, alt, url, title = match.group(1), match.group(2), match.group(3), match.group(4)
        replacement = mapper(url)
        if not replacement or replacement == url:
            return match.group(0)
        count += 1
        title_part = f' "{title}"' if title else ""
        return f"{bang}[{alt}]({replacement}{title_part})"

    return _LINK_RE.sub(_sub, markdown or ""), count


def local_file_links(markdown: str) -> list[tuple[str, str]]:
    """Local references to backup attachments: [(local_path, full_match)].

    Used by ``restore`` to find the attachments that must be re-uploaded to
    Notion. ``local_path`` looks like ``/backup/files/<hash>/<name>``.
    """
    return [(m.group(1), m.group(0)) for m in _LOCAL_FILE_RE.finditer(markdown or "")]


def strip_local_link(match: str, alt: str) -> str:
    """Plain-text replacement for a local link (used by restore)."""
    return alt if alt else ""


# ---------------------------------------------------------------------------
# Post-processing: fix Notion-specific markup for standard Markdown viewers
# ---------------------------------------------------------------------------


def _hex32_to_uuid(hex32: str) -> str:
    """Converts a 32-char hex string to a standard UUID with dashes."""
    return f"{hex32[:8]}-{hex32[8:12]}-{hex32[12:16]}-{hex32[16:20]}-{hex32[20:]}"


def prepend_title(markdown: str, title: str) -> str:
    """Prepends ``# Title`` at the top so the page has a heading in any viewer."""
    if not title:
        return markdown
    return f"# {title}\n\n{markdown}"


def convert_page_tags(markdown: str, page_url_to_path) -> str:
    """Replaces ``<page url="...">Title</page>`` with standard Markdown links
    pointing to the local backup copy.

    ``page_url_to_path(uuid: str) -> str | None`` is called with the UUID of
    the target page; if it returns a path, a ``[title](path.md)`` link is
    emitted.  Otherwise the original Notion URL is kept as a fallback.
    """

    def _repl(m: re.Match) -> str:
        hex_id = m.group(1)
        uuid = _hex32_to_uuid(hex_id)
        title = m.group(2).strip()
        path = page_url_to_path(uuid)
        if path:
            return f"[{title}]({path}.md)"
        return f"[{title}](https://app.notion.com/p/{hex_id})"

    return _PAGE_TAG_RE.sub(_repl, markdown)


def convert_database_tags(markdown: str, ds_url_to_path) -> str:
    """Replaces ``<database ... data-source-url="collection://UUID">Title</database>``
    with Markdown links to the local database directory.

    ``ds_url_to_path(uuid: str) -> str | None`` is called with the data source
    UUID extracted from the ``collection://`` URI.
    """

    def _repl(m: re.Match) -> str:
        ds_uuid = m.group(1)
        title = m.group(2).strip()
        path = ds_url_to_path(ds_uuid)
        if path:
            return f"[🗄 {title}]({path}/)"
        return f"🗄 {title}"

    return _DATABASE_TAG_RE.sub(_repl, markdown)


def convert_callouts(markdown: str) -> str:
    """Converts Notion ``<callout>`` blocks to blockquotes.

    The icon and colour are lost (they are Notion-specific), but the text
    content is preserved inside a ``> `` blockquote that renders everywhere.
    """

    def _repl(m: re.Match) -> str:
        content = m.group(1).strip()
        lines = content.split("\n")
        return "\n".join(
            f"> {line}" if line.strip() else ">" for line in lines
        )

    return _CALLOUT_RE.sub(_repl, markdown)


def remove_toc(markdown: str) -> str:
    """Strips ``<table-of-contents />`` placeholders (Notion-only feature)."""
    return _TOC_RE.sub("", markdown)


def post_process(
    markdown: str,
    *,
    title: str = "",
    page_url_to_path=None,
    ds_url_to_path=None,
) -> str:
    """Applies all Notion→standard-Markdown transformations.

    Parameters:
        title: Page title to prepend as ``# Title``.
        page_url_to_path: ``(uuid: str) -> str | None`` for child page links.
        ds_url_to_path: ``(uuid: str) -> str | None`` for database links.
    """
    if title:
        markdown = prepend_title(markdown, title)
    markdown = convert_callouts(markdown)
    markdown = remove_toc(markdown)
    if page_url_to_path:
        markdown = convert_page_tags(markdown, page_url_to_path)
    if ds_url_to_path:
        markdown = convert_database_tags(markdown, ds_url_to_path)
    return markdown
