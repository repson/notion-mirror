"""Full workspace inventory: search + tree walk + reconciliation.

Notion's search is NOT a complete inventory (its index lags on new pages,
freshly shared ones, or pages moved to the trash). This module combines it
with:

1. Paginated search of pages and data sources.
2. Reconciliation against the previous manifest: everything that used to be
   there but no longer shows up is verified one by one against the API (404?
   in the trash? still alive?).
3. A tree walk: the children of every new/edited/root page are walked by
   reading their child blocks (creating or deleting a child page edits the
   parent, so the walk always finds the changes). Databases found this way
   are registered with their data sources and rows.

The result is the definitive list of live pages and databases plus the list
of confirmed removals.
"""

from __future__ import annotations

import logging
from collections import deque

from .http import NotionAPIError
from .model import Inventory, database_title, data_source_title, is_trashed, page_title

log = logging.getLogger("notion-mirror")


def build_inventory(api, prev: dict) -> Inventory:
    """:param api: an object with the surface of ``NotionAPI`` (easy to fake).
    :param prev: the manifest of the previous run (empty on the first one).
    """
    inv = Inventory()
    prev_pages: dict = prev.get("pages", {})
    prev_ds: dict = prev.get("data_sources", {})

    # 1) Search for pages and data sources.
    log.info("Searching pages via the Notion API…")
    page_count = 0
    for page in api.search_objects("page"):
        _add_page(inv, page, prev_pages)
        page_count += 1
    log.info("Found %d pages in search results", page_count)

    log.info("Searching databases (data sources) via the Notion API…")
    ds_count = 0
    for ds in api.search_objects("data_source"):
        ds_count += 1
        if "parent" not in ds:
            # Partial result from the index: fetch the full object.
            full = api.get_data_source(ds["id"])
            if full is None:
                inv.warnings.append(f"Data source {ds['id']} returned by search but not accessible")
                continue
            ds = full
        _add_data_source(inv, ds, prev_ds)
    log.info("Found %d data sources in search results", ds_count)

    # 2) Reconciliation against the previous manifest (detects deletions and
    #    search-index gaps). Runs before the walk so recovered pages are
    #    walked too.
    prev_page_count = len(prev_pages)
    if prev_page_count:
        log.info("Reconciling %d previously backed up pages against the API…", prev_page_count)
    reconciled = 0
    removed_count = 0
    for pid, entry in sorted(prev_pages.items()):
        if pid in inv.pages:
            continue
        reconciled += 1
        if reconciled % 50 == 0:
            log.info("Reconciliation: %d/%d pages checked", reconciled, prev_page_count)
        page = api.get_page(pid)
        if page is None:
            inv.removed.append({
                "kind": "page", "id": pid,
                "title": entry.get("title", ""),
                "reason": "deleted_or_no_access",
            })
            removed_count += 1
        elif is_trashed(page):
            inv.removed.append({
                "kind": "page", "id": pid,
                "title": entry.get("title", ""),
                "reason": "in_trash",
            })
            removed_count += 1
        else:
            inv.warnings.append(
                f"Page '{entry.get('title', pid)}' did not appear in the enumeration "
                f"but is still alive (stale index); recovered by reconciliation"
            )
            _add_page(inv, page, prev_pages)
    if prev_page_count:
        log.info("Reconciliation complete: %d pages verified, %d confirmed removed",
                 reconciled, removed_count)

    prev_ds_count = len(prev_ds)
    if prev_ds_count:
        log.info("Reconciling %d previously backed up data sources…", prev_ds_count)
    ds_reconciled = 0
    for dsid, entry in sorted(prev_ds.items()):
        if dsid in inv.data_sources:
            continue
        ds_reconciled += 1
        ds = api.get_data_source(dsid)
        if ds is None:
            inv.removed.append({
                "kind": "data_source", "id": dsid,
                "title": entry.get("title", ""),
                "reason": "deleted_or_no_access",
            })
        elif is_trashed(ds):
            inv.removed.append({
                "kind": "data_source", "id": dsid,
                "title": entry.get("title", ""),
                "reason": "in_trash",
            })
        else:
            inv.warnings.append(
                f"Database '{entry.get('title', dsid)}' did not appear in the enumeration "
                f"but is still alive (stale index); recovered by reconciliation"
            )
            _add_data_source(inv, ds, prev_ds, api=api)
    if prev_ds_count:
        log.info("Data source reconciliation complete: %d verified", ds_reconciled)

    # 3) Rows of the known data sources + metadata of their databases.
    for ds_id in list(inv.data_sources):
        _ensure_parent_database(inv, api, ds_id)

    # 4) Tree walk: children of new, edited or root pages. Repeats until
    #    stable because registering a database adds rows (new pages) that
    #    must be walked too.
    walked: set[str] = set()
    walked_total = 0
    while True:
        to_walk = deque(
            p for p in inv.pages.values()
            if p["id"] not in walked and _walk_worthy(p, prev_pages)
        )
        if not to_walk:
            break
        walk_count = len(to_walk)
        log.info("Walking %d pages for child blocks (new/edited/root pages)…", walk_count)
        while to_walk:
            page = to_walk.popleft()
            pid = page["id"]
            if pid in walked:
                continue
            walked.add(pid)
            walked_total += 1
            if walked_total % 50 == 0:
                log.info("Tree walk: %d pages processed, %d remaining in queue",
                         walked_total, len(to_walk))
            title = page_title(page) or pid
            for block in api.get_block_children(pid):
                btype = block.get("type")
                if btype == "child_page":
                    cid = block["id"]
                    if cid not in inv.pages:
                        child = api.get_page(cid)
                        if child is not None and _add_page(inv, child, prev_pages):
                            log.debug("  Found child page '%s' under '%s'",
                                      page_title(child) or cid[:8], title[:40])
                elif btype == "child_database":
                    if block["id"] not in inv.databases:
                        _register_database(inv, api, block["id"], warn_title=title)
                        log.debug("  Found child database under '%s'", title[:40])
    if walked_total:
        log.info("Tree walk complete: %d pages walked", walked_total)

    for warning in inv.warnings:
        log.warning("%s", warning)
    log.info(
        "Inventory: %d pages, %d databases (data sources), %d confirmed removed",
        len(inv.pages), len(inv.data_sources), len(inv.removed),
    )
    return inv


# ------------------------------------------------------------------- helpers


def _walk_worthy(page: dict, prev_pages: dict) -> bool:
    """Should the children of this page be walked in this run?

    - New (not in the previous manifest) → yes.
    - Edited (last_edited_time differs) → yes (may have new/removed children).
    - Workspace root → always (cheap, and protects against index gaps).
    """
    pid = page["id"]
    entry = prev_pages.get(pid)
    if entry is None:
        return True
    if entry.get("last_edited_time") != page.get("last_edited_time"):
        return True
    return (page.get("parent") or {}).get("type") == "workspace"


def _add_page(inv: Inventory, page: dict, prev_pages: dict) -> bool:
    """Adds a live page. Returns False if it is in the trash (and records it
    as a removal if it was previously backed up)."""
    pid = page["id"]
    if pid in inv.pages:
        return False
    if is_trashed(page):
        if pid in prev_pages:
            inv.removed.append({
                "kind": "page", "id": pid,
                "title": prev_pages[pid].get("title", ""),
                "reason": "in_trash",
            })
        return False
    inv.pages[pid] = page
    return True


def _add_data_source(inv: Inventory, ds: dict, prev_ds: dict, api=None) -> bool:
    """Registers a live data source and immediately enumerates its rows.

    Rows are pages: added here, they enter the tree walk and the backup as
    regular pages.
    """
    dsid = ds["id"]
    if dsid in inv.data_sources:
        return False
    if is_trashed(ds):
        if dsid in prev_ds:
            inv.removed.append({
                "kind": "data_source", "id": dsid,
                "title": prev_ds[dsid].get("title", ""),
                "reason": "in_trash",
            })
        return False
    inv.data_sources[dsid] = ds
    if api is not None:
        _enumerate_rows(inv, api, dsid)
    return True


def _enumerate_rows(inv: Inventory, api, ds_id: str) -> None:
    try:
        rows = list(api.query_data_source(ds_id))
    except NotionAPIError as exc:
        if exc.status == 403 and exc.code == "restricted_resource":
            inv.warnings.append(
                f"Data source {ds_id} returned restricted_resource; skipping rows"
            )
            return
        raise
    inv.rows[ds_id] = rows
    for row in rows:
        rid = row["id"]
        if rid not in inv.pages and not is_trashed(row):
            inv.pages[rid] = row


def _register_database(inv: Inventory, api, database_id: str, warn_title: str = "") -> None:
    """Registers a database (by child_database block id) with its data sources."""
    db = api.get_database(database_id)
    if db is None:
        inv.warnings.append(
            f"Database {database_id} (child of '{warn_title}') not accessible; skipped"
        )
        return
    if is_trashed(db):
        return
    inv.databases[database_id] = db
    for ds_ref in db.get("data_sources", []):
        ds_id = ds_ref.get("id")
        if ds_id and ds_id not in inv.data_sources:
            ds = api.get_data_source(ds_id)
            if ds is not None:
                _add_data_source(inv, ds, {}, api=api)


def _ensure_parent_database(inv: Inventory, api, ds_id: str) -> None:
    """Gets the database object a known data source belongs to, and enumerates
    its rows if not done yet (rows are pages)."""
    ds = inv.data_sources[ds_id]
    if ds_id not in inv.rows:
        _enumerate_rows(inv, api, ds_id)
    parent = ds.get("parent") or {}
    db_id = parent.get("database_id") or (ds.get("database_parent") or {}).get("database_id")
    if not db_id:
        return
    if db_id in inv.databases:
        return
    if parent.get("type") == "data_source_id":
        # Synced data source: its container is another data source.
        outer = parent.get("data_source_id")
        if outer and outer not in inv.data_sources:
            outer_ds = api.get_data_source(outer)
            if outer_ds is not None:
                _add_data_source(inv, outer_ds, {}, api=api)
        return
    _register_database(inv, api, db_id, warn_title=data_source_title(ds))
