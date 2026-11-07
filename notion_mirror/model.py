"""Shared types and helpers to read API objects."""

from __future__ import annotations

from dataclasses import dataclass, field


def plain_text_of(rich_text: list | None) -> str:
    """Concatenates the plain text of a rich text array."""
    return "".join(rt.get("plain_text", "") for rt in rich_text or [])


def page_title(page: dict) -> str:
    """A page's title, from its properties."""
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return plain_text_of(prop.get("title"))
    return ""


def data_source_title(ds: dict) -> str:
    return plain_text_of(ds.get("title"))


def database_title(db: dict) -> str:
    return plain_text_of(db.get("title"))


def is_trashed(obj: dict) -> bool:
    return bool(obj.get("in_trash") or obj.get("is_archived") or obj.get("archived"))


def parent_id_of(obj: dict) -> str | None:
    """The parent's id (page, block, database or data source), or None for roots."""
    parent = obj.get("parent") or {}
    for key in ("page_id", "block_id", "database_id", "data_source_id", "agent_id"):
        if key in parent:
            return parent[key]
    return None


@dataclass
class Inventory:
    """Complete, verified inventory of the accessible content.

    - ``pages`` / ``data_sources`` / ``databases``: live objects (not in the
      trash), indexed by id.
    - ``rows``: rows of each data source (page objects), kept so enumeration
      and backup can reuse them without re-querying.
    - ``removed``: pages/databases confirmed deleted or inaccessible
      (to be removed from the mirror).
    - ``warnings``: incidents (stale index, orphaned pages...).
    """

    pages: dict[str, dict] = field(default_factory=dict)
    data_sources: dict[str, dict] = field(default_factory=dict)
    databases: dict[str, dict] = field(default_factory=dict)
    rows: dict[str, list] = field(default_factory=dict)
    removed: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
