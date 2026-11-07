"""Stable slugs for file and directory names."""

from __future__ import annotations

import re
import unicodedata


def slugify(text: str, max_length: int = 72) -> str:
    """Turns a title into a safe path component.

    - Strips accents (``Café`` → ``cafe``) to avoid encoding trouble.
    - Collapses any non-alphanumeric run into a single ``-``.
    - Truncates to ``max_length`` so paths stay manageable.
    - Returns ``untitled`` if nothing useful is left (emoji-only, empty...).

    Uniqueness is guaranteed by the short-id suffix the caller appends.
    """
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9._]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-._")
    if len(s) > max_length:
        s = s[:max_length].rstrip("-._")
    return s or "untitled"
