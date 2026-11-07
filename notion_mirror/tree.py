"""Resolves the path of each page/database inside ``backup/pages/``.

The repository tree mirrors the Notion hierarchy using each object's
``parent`` (recomputed on every run: page moves do not touch
``last_edited_time`` and must still be reflected).

Placement rules:
- ``parent workspace``            → ``pages/``
- ``parent page_id``              → that page's children directory (its path)
- ``parent data_source_id``       → the database's directory (database rows)
- ``parent database_id``          → the database's directory (legacy model)
- ``parent block_id``             → the page containing the block (flattened)
- data source                     → next to its database's parent, own name

Component name: ``<slug(title)>-<id8>`` → stable, unique paths; a title
change is a rename for git.
"""

from __future__ import annotations

import logging

from .model import database_title, data_source_title, page_title
from .slugify import slugify

log = logging.getLogger("notion-mirror")

PAGES_ROOT = "pages"
ORPHAN_DIR = f"{PAGES_ROOT}/_orphan"


def component(title: str, object_id: str) -> str:
    return f"{slugify(title)}-{object_id.replace('-', '')[:8]}"


def build_paths(inv, resolve_block=None):
    """Computes backup-relative paths for every page and data source.

    :param inv: an ``Inventory``.
    :param resolve_block: callable ``block_id -> block | None`` to resolve
        pages whose parent is an unknown block.
    :return: ``(page_paths, data_source_paths, warnings)``
    """
    warnings: list[str] = []
    memo_page: dict[str, str] = {}
    memo_ds: dict[str, str] = {}
    resolving: set[str] = set()

    def orphan(what: str, oid: str) -> str:
        warnings.append(f"{what} {oid} has no resolvable parent → {ORPHAN_DIR}")
        return f"{ORPHAN_DIR}/{component('unplaced', oid)}"

    def page_path(pid: str) -> str:
        if pid in memo_page:
            return memo_page[pid]
        if pid in resolving:
            return orphan("page (cycle)", pid)
        page = inv.pages.get(pid)
        if page is None:
            return orphan("parent page not accessible", pid)
        resolving.add(pid)
        try:
            parent = page.get("parent") or {}
            ptype = parent.get("type")
            if ptype == "workspace":
                base = PAGES_ROOT
            elif ptype == "page_id":
                base = page_path(parent["page_id"])
            elif ptype == "data_source_id":
                base = ds_dir(parent["data_source_id"])
            elif ptype == "database_id":
                base = db_dir(parent["database_id"])
            elif ptype == "block_id":
                base = block_dir(parent["block_id"])
            else:
                base = orphan("page", pid)
                memo_page[pid] = base
                return base
            path = f"{base}/{component(page_title(page), pid)}"
        finally:
            resolving.discard(pid)
        memo_page[pid] = path
        return path

    def ds_dir(dsid: str) -> str:
        if dsid in memo_ds:
            return memo_ds[dsid]
        if dsid in resolving:
            return orphan("database (cycle)", dsid)
        ds = inv.data_sources.get(dsid)
        if ds is None:
            return orphan("database not accessible", dsid)
        resolving.add(dsid)
        try:
            parent = ds.get("parent") or {}
            if parent.get("type") == "data_source_id":
                # Synced data source: placed inside its container.
                base = ds_dir(parent["data_source_id"])
            else:
                gp = ds.get("database_parent") or {}
                gptype = gp.get("type")
                if gptype == "page_id":
                    base = page_path(gp["page_id"])
                elif gptype == "workspace" or gptype is None:
                    base = PAGES_ROOT
                elif gptype == "block_id":
                    base = block_dir(gp["block_id"])
                elif gptype == "database_id":
                    base = db_dir(gp["database_id"])
                elif gptype == "data_source_id":
                    base = ds_dir(gp["data_source_id"])
                else:
                    base = PAGES_ROOT
                    warnings.append(f"Database {dsid} with unknown parent → root")
            path = f"{base}/{component(data_source_title(ds) or 'database', dsid)}"
        finally:
            resolving.discard(dsid)
        memo_ds[dsid] = path
        return path

    def db_dir(dbid: str) -> str:
        db = inv.databases.get(dbid)
        if db is None:
            return orphan("database not accessible", dbid)
        # The directory of one of its data sources (usually exactly one).
        for ds in inv.data_sources.values():
            if (ds.get("parent") or {}).get("database_id") == dbid:
                return ds_dir(ds["id"])
        for ds in inv.data_sources.values():
            if (ds.get("database_parent") or {}).get("database_id") == dbid:
                return ds_dir(ds["id"])
        # Database without accessible data sources: place it by its own parent.
        parent = db.get("parent") or {}
        ptype = parent.get("type")
        if ptype == "page_id" and parent["page_id"] in inv.pages:
            base = page_path(parent["page_id"])
        elif ptype == "workspace":
            base = PAGES_ROOT
        elif ptype == "block_id":
            base = block_dir(parent["block_id"])
        else:
            return orphan("database without data sources", dbid)
        return f"{base}/{component(database_title(db) or 'database', dbid)}"

    def block_dir(bid: str) -> str:
        # A child_page block has the same id as the page it represents;
        # its children live in that page's directory.
        if bid in inv.pages:
            return page_path(bid)
        if bid in inv.databases:
            return db_dir(bid)
        block = resolve_block(bid) if resolve_block else None
        if block is None:
            return orphan("parent block not accessible", bid)
        parent = block.get("parent") or {}
        ptype = parent.get("type")
        if ptype == "page_id":
            return page_path(parent["page_id"])
        if ptype == "block_id":
            return block_dir(parent["block_id"])
        if ptype == "data_source_id":
            return ds_dir(parent["data_source_id"])
        if ptype == "workspace":
            return PAGES_ROOT
        return orphan("block", bid)

    for pid in inv.pages:
        page_path(pid)
    for dsid in inv.data_sources:
        ds_dir(dsid)

    for warning in warnings:
        log.warning("%s", warning)
    return memo_page, memo_ds, warnings


def page_sidecar_files(rel_path: str) -> list[str]:
    """Paths of the files that represent a page in the mirror."""
    return [rel_path + ext for ext in (".md", ".json", ".blocks.json")]


def page_children_dir(rel_path: str) -> str:
    """Directory where the children of a page live (its own path)."""
    return rel_path
