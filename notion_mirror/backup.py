"""Backup orchestrator: syncs the local mirror with Notion.

Mirror semantics: the ``backup/`` directory reflects the current state of
the workspace. Whatever is deleted in Notion (verified against the API) is
deleted from the mirror; git keeps the history.

Typical run::

    notion-mirror backup
    git add -A && git commit -m "backup: 2026-11-01" && git push
"""

from __future__ import annotations

import csv
import io
import json
import logging
import shutil
import time
from pathlib import Path

from . import manifest as manifest_mod
from .api import NotionAPI, _is_restricted_resource
from .config import Config
from .files import FileStore
from .http import NotionAPIError, NotionHTTP
from .inventory import build_inventory
from .manifest import dumps, write_if_different
from .markdown import post_process, rewrite_links
from .model import page_title
from .sidecar import build_database_sidecar, build_page_sidecar, sanitize_comments
from .tree import build_paths
from .urls import is_notion_file_url

log = logging.getLogger("notion-mirror")

_DS_SIDECAR = "data_source.json"
_ROWS_JSON = "rows.json"
_ROWS_CSV = "rows.csv"


def run_backup(cfg: Config) -> int:
    started = time.monotonic()

    if not cfg.token:
        log.error("Missing token: use --token, the NOTION_TOKEN environment variable or the .env file")
        return 1

    root = cfg.output
    root.mkdir(parents=True, exist_ok=True)
    (root / "pages").mkdir(exist_ok=True)
    (root / "files").mkdir(exist_ok=True)
    cfg.cache.mkdir(parents=True, exist_ok=True)

    http = NotionHTTP(cfg.token, requests_per_second=cfg.requests_per_second)
    api = NotionAPI(http)
    file_store = FileStore(
        root, cfg.cache / "files.json", http.download_to, link_prefix=f"/{root.name}/"
    )

    stats = {
        "pages_total": 0, "pages_new": 0, "pages_changed": 0, "pages_unchanged": 0,
        "ds_total": 0, "ds_changed": 0, "files_total": 0, "files_new": 0,
    }
    # Pages whose fetch failed: they keep the previous timestamp in the
    # manifest so the next run retries them.
    failed: dict[str, str] = {}

    try:
        prev = manifest_mod.load(root)
        prev_pages: dict = prev.get("pages", {})
        prev_ds: dict = prev.get("data_sources", {})

        log.info("Enumerating the workspace (search + tree walk + reconciliation)…")
        inv = build_inventory(api, prev)
        stats["pages_total"] = len(inv.pages)
        stats["ds_total"] = len(inv.data_sources)
        log.info("Resolving filesystem paths for %d pages and %d databases…",
                 stats["pages_total"], stats["ds_total"])
        page_paths, ds_paths, warnings = build_paths(inv, api.get_block)
        if inv.removed:
            log.info("Preparing to remove %d deleted items from the mirror…", len(inv.removed))

        # 1) Confirmed removals: delete from the mirror (git keeps history).
        _apply_removals(root, inv.removed, prev)

        # 2) Pages.
        page_files: dict[str, list] = {}

        # Path resolvers for post-processing <page>/<database> tags.
        def _page_url_to_path(uuid: str) -> str | None:
            """Maps a Notion page UUID to its local mirror path."""
            return page_paths.get(uuid)

        def _ds_url_to_path(uuid: str) -> str | None:
            """Maps a data source UUID to its local mirror directory path."""
            return ds_paths.get(uuid)

        ordered = sorted(inv.pages.items(), key=lambda kv: page_paths[kv[0]])
        pages_total = len(ordered)
        if pages_total:
            log.info("Syncing %d pages…", pages_total)
        for index, (pid, page) in enumerate(ordered, start=1):
            rel = page_paths[pid]
            prev_entry = prev_pages.get(pid) or {}
            old_rel = prev_entry.get("path")
            if old_rel and old_rel != rel:
                _move_page_files(root, old_rel, rel)
            _sync_page(
                api, inv, root, pid, page, rel, prev_entry, cfg,
                file_store, page_files, stats, failed,
                page_url_to_path=_page_url_to_path,
                ds_url_to_path=_ds_url_to_path,
            )
            if index % 25 == 0 or index == len(ordered):
                log.info("Pages: %d/%d", index, len(ordered))

        # 3) Databases (data sources).
        ds_files: dict[str, list] = {}
        ds_total = len(inv.data_sources)
        if ds_total:
            log.info("Syncing %d databases…", ds_total)
        ds_index = 0
        for dsid, ds in sorted(inv.data_sources.items(), key=lambda kv: ds_paths[kv[0]]):
            ds_index += 1
            _sync_data_source(
                api, inv, root, dsid, ds, ds_paths[dsid],
                prev_ds.get(dsid) or {}, cfg, file_store, ds_files, stats, failed,
            )
            if ds_index % 10 == 0 or ds_index == ds_total:
                log.info("Databases: %d/%d", ds_index, ds_total)

        # 4) Orphaned attachments (referenced by nothing) → out of the mirror.
        log.info("Collecting referenced files and cleaning up orphans…")
        referenced: set[str] = set()
        for files in page_files.values():
            referenced.update(files)
        for files in ds_files.values():
            referenced.update(files)
        files_gc = file_store.gc(referenced)
        stats["files_total"] = len(referenced)
        stats["files_new"] = file_store.downloaded

        # 5) Manifest (deterministic) and report (with date).
        new_manifest = manifest_mod.build(
            inv, page_paths, ds_paths, page_files, ds_files, failed
        )
        manifest_mod.write_if_different(root / "manifest.json", dumps(new_manifest))
        report = manifest_mod.build_report(
            started_at=started,
            duration=time.monotonic() - started,
            requests=http.requests,
            stats=stats,
            removed=inv.removed,
            warnings=warnings,
            files_gc=files_gc,
        )
        (root / "report.md").write_text(report, encoding="utf-8")
    finally:
        file_store.save_cache()
        http.close()

    changed = (
        stats["pages_new"] + stats["pages_changed"] + len(inv.removed)
        + stats["ds_changed"] + files_gc + file_store.downloaded
    )
    log.info(
        "Backup finished in %.0fs · %d requests · %d pages, %d databases, %d files",
        time.monotonic() - started, http.requests,
        stats["pages_total"], stats["ds_total"], stats["files_total"],
    )
    log.info(
        "Changes: %d new, %d modified, %d removed, %d new files, %d orphaned",
        stats["pages_new"], stats["pages_changed"], len(inv.removed),
        stats["files_new"], files_gc,
    )
    if changed:
        log.info("Next: git add -A && git commit -m 'backup: %s' && git push",
                 time.strftime("%Y-%m-%d %H:%M"))
    else:
        log.info("No changes: nothing to commit (the manifest is unchanged)")
    return 0


# -------------------------------------------------------------------- pages


def _sync_page(
    api, inv, root: Path, pid: str, page: dict, rel: str,
    prev_entry: dict, cfg: Config, file_store: FileStore,
    page_files: dict, stats: dict, failed: dict,
    page_url_to_path=None, ds_url_to_path=None,
) -> None:
    """Syncs one page: Markdown, sidecar, comments and attachments."""
    md_path = root / (rel + ".md")
    sc_path = root / (rel + ".json")
    blocks_path = root / (rel + ".blocks.json")

    prev_edited = prev_entry.get("last_edited_time")
    changed = (
        cfg.force
        or prev_edited != page.get("last_edited_time")
        or not md_path.exists()
    )

    sink: list[str] = []
    try:
        if changed:
            ptitle = page_title(page) or pid[:8]
            log.debug("Downloading '%s' (%s)…", ptitle, pid[:8])
            markdown, raw_blocks = _fetch_markdown(api, pid)
            rewritten, _ = rewrite_links(
                markdown, lambda url: _file_mapper(url, file_store, sink)
            )
            rewritten = post_process(
                rewritten,
                title=ptitle,
                page_url_to_path=page_url_to_path,
                ds_url_to_path=ds_url_to_path,
            )
            if not rewritten.endswith("\n"):
                rewritten += "\n"
            write_if_different(md_path, rewritten)
            if raw_blocks:
                write_if_different(blocks_path, dumps(raw_blocks))
            elif blocks_path.exists():
                blocks_path.unlink()
            if prev_entry:
                stats["pages_changed"] += 1
            else:
                stats["pages_new"] += 1
        else:
            stats["pages_unchanged"] += 1

        comments = None
        if cfg.comments == "always" or (cfg.comments == "on-change" and changed):
            comments = sanitize_comments(
                api.get_page_comments(pid), api.get_user, file_store, sink
            )

        if changed or comments is not None or not sc_path.exists():
            old_sidecar = _load_json(sc_path)
            if not changed and old_sidecar:
                # Keep the attachments from the previous sidecar (unchanged page).
                sink.extend(old_sidecar.get("files", []))
            sidecar = build_page_sidecar(page, inv, api.get_user, file_store, comments, sink)
            if comments is None and old_sidecar and "comments" in old_sidecar:
                sidecar["comments"] = old_sidecar["comments"]
            write_if_different(sc_path, dumps(sidecar))
    except NotionAPIError as exc:
        if exc.status == 401 or (exc.status == 403 and exc.code != "restricted_resource"):
            raise
        if prev_edited:
            failed[pid] = prev_edited
        log.warning("Error syncing '%s' (%s): %s — will retry on the next run",
                    page_title(page) or pid, pid, exc)

    page_files[pid] = (
        sorted(set(sink)) if changed
        else (sorted(set(sink)) if sink else list(prev_entry.get("files", [])))
    )


def _file_mapper(url: str, file_store: FileStore, sink: list) -> str | None:
    """Rewrites Notion-internal file URLs to absolute local paths."""
    if not is_notion_file_url(url):
        return None
    local = file_store.ensure(url, sink)
    if local is None:
        return None
    return file_store.absolute_link(local)


def _fetch_markdown(api, page_id: str) -> tuple[str, dict]:
    """A page's Markdown, including truncated sub-trees.

    Pages with ~20k+ records return ``unknown_block_ids``: they are
    re-requested through the same endpoint. Those the API cannot render (404
    for permissions) are stored as raw JSON blocks so nothing is lost.
    """
    response = api.get_page_markdown(page_id)
    markdown = response.get("markdown", "")
    unknown = list(response.get("unknown_block_ids") or [])
    raw_blocks: dict[str, dict] = {}
    seen: set[str] = set()
    while unknown:
        block_id = unknown.pop(0)
        if block_id in seen:
            continue
        seen.add(block_id)
        try:
            sub = api.get_page_markdown(block_id)
            if sub.get("markdown"):
                markdown += f"\n\n<!-- subtree {block_id} -->\n\n{sub['markdown']}"
            unknown.extend(sub.get("unknown_block_ids") or [])
        except NotionAPIError as exc:
            if exc.status == 404 or _is_restricted_resource(exc):
                block = api.get_block(block_id)
                if block is not None:
                    raw_blocks[block_id] = block
            else:
                raise
    return markdown, raw_blocks


def _move_page_files(root: Path, old_rel: str, new_rel: str) -> None:
    """Moves the files of a page whose path changed (move/rename)."""
    for suffix in (".md", ".json", ".blocks.json"):
        src = root / (old_rel + suffix)
        if src.exists():
            dst = root / (new_rel + suffix)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
    src_dir = root / old_rel
    if src_dir.is_dir():
        dst_dir = root / new_rel
        dst_dir.parent.mkdir(parents=True, exist_ok=True)
        if not dst_dir.exists():
            shutil.move(str(src_dir), str(dst_dir))


# ------------------------------------------------------------- data sources


def _sync_data_source(
    api, inv, root: Path, dsid: str, ds: dict, rel: str, prev_entry: dict,
    cfg: Config, file_store: FileStore, ds_files: dict, stats: dict, failed: dict,
) -> None:
    ds_dir = root / rel
    sidecar_path = ds_dir / _DS_SIDECAR
    old_rel = prev_entry.get("path")
    if old_rel and old_rel != rel and (root / old_rel).is_dir():
        ds_dir.parent.mkdir(parents=True, exist_ok=True)
        if not ds_dir.exists():
            shutil.move(str(root / old_rel), str(ds_dir))

    prev_edited = prev_entry.get("last_edited_time")
    changed = (
        cfg.force
        or prev_edited != ds.get("last_edited_time")
        or not sidecar_path.exists()
    )

    sink: list[str] = []
    try:
        if changed:
            rows = inv.rows.get(dsid, [])
            _write_rows(ds, rows, ds_dir, inv, file_store, api.get_user, sink)
            database = inv.databases.get((ds.get("parent") or {}).get("database_id"))
            sidecar = build_database_sidecar(database, ds, file_store, api.get_user, sink)
            write_if_different(sidecar_path, dumps(sidecar))
            stats["ds_changed"] += 1
        else:
            sink.extend(prev_entry.get("files", []))
    except NotionAPIError as exc:
        if exc.status == 401 or (exc.status == 403 and exc.code != "restricted_resource"):
            raise
        if prev_edited:
            failed[f"ds:{dsid}"] = prev_edited
        log.warning("Error syncing database %s: %s — will retry on the next run", dsid, exc)

    ds_files[dsid] = (
        sorted(set(sink)) if changed
        else (sorted(set(sink)) if sink else list(prev_entry.get("files", [])))
    )


def _write_rows(ds: dict, rows: list, ds_dir: Path, inv, file_store, users_fn, sink) -> None:
    """Writes ``rows.json`` (faithful) and ``rows.csv`` (flat, importable)."""
    from .sidecar import _sanitize_properties

    ds_dir.mkdir(parents=True, exist_ok=True)
    schema = ds.get("properties") or {}

    sanitized_rows = []
    for row in sorted(rows, key=lambda r: (r.get("created_time") or "", r["id"])):
        sanitized_rows.append({
            "id": row["id"],
            "url": row.get("url"),
            "created_time": row.get("created_time"),
            "last_edited_time": row.get("last_edited_time"),
            "properties": _sanitize_properties(row.get("properties"), inv, file_store, users_fn, sink),
        })
    write_if_different(ds_dir / _ROWS_JSON, dumps({"rows": sanitized_rows}))

    columns = ["id", "url", "created_time", "last_edited_time"]
    columns += [prop.get("name") or name for name, prop in schema.items()]
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in sanitized_rows:
        props_raw = (next(
            (r for r in rows if r["id"] == row["id"]), {}
        )).get("properties") or {}
        values = [
            row.get("id"), row.get("url", ""),
            row.get("created_time", ""), row.get("last_edited_time", ""),
        ]
        for name, prop in schema.items():
            values.append(_csv_cell(props_raw.get(name), users_fn, inv))
        writer.writerow(values)
    write_if_different(ds_dir / _ROWS_CSV, buffer.getvalue())


def _csv_cell(prop: dict | None, users_fn, inv) -> str:
    """Flattens a property value for the CSV."""
    if not prop:
        return ""
    ptype = prop.get("type")
    if ptype in ("title", "rich_text"):
        return "".join(rt.get("plain_text", "") for rt in prop.get(ptype) or [])
    if ptype == "number":
        return "" if prop.get("number") is None else str(prop.get("number"))
    if ptype in ("select", "status"):
        return (prop.get(ptype) or {}).get("name", "")
    if ptype == "multi_select":
        return ", ".join(o.get("name", "") for o in prop.get("multi_select") or [])
    if ptype == "date":
        date = prop.get("date") or {}
        if not date.get("start"):
            return ""
        end = date.get("end")
        return f"{date['start']} → {end}" if end else str(date["start"])
    if ptype == "checkbox":
        return "true" if prop.get("checkbox") else "false"
    if ptype in ("url", "email", "phone_number"):
        return prop.get(ptype) or ""
    if ptype == "people":
        return ", ".join(
            ((users_fn(p.get("id")) or {}).get("name") or p.get("id", ""))
            for p in prop.get("people") or []
        )
    if ptype == "files":
        parts = []
        for f in prop.get("files") or []:
            ftype = f.get("type")
            inner = f.get(ftype) if ftype in ("file", "external") else None
            parts.append(f.get("name") or (inner or {}).get("name") or "")
        return ", ".join(parts)
    if ptype == "relation":
        out = []
        for related in prop.get("relation") or []:
            rid = related.get("id")
            page = inv.pages.get(rid) if inv else None
            out.append(page_title(page) if page else rid)
        return ", ".join(out)
    if ptype == "unique_id":
        uid = prop.get("unique_id") or {}
        prefix = uid.get("prefix")
        return f"{prefix}-{uid.get('number')}" if prefix else str(uid.get("number", ""))
    value = prop.get(ptype)
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------- removals


def _apply_removals(root: Path, removed: list[dict], prev: dict) -> None:
    """Deletes confirmed-removed pages/databases from the mirror."""
    for item in removed:
        kind = item.get("kind")
        oid = item.get("id")
        prev_map = (prev.get("pages") or {}) if kind == "page" else (prev.get("data_sources") or {})
        entry = prev_map.get(oid) or {}
        rel = entry.get("path")
        if not rel:
            continue
        if kind == "page":
            for suffix in (".md", ".json", ".blocks.json"):
                target = root / (rel + suffix)
                if target.exists():
                    target.unlink()
            children = root / rel
            if children.is_dir():
                shutil.rmtree(children)
        else:
            target = root / rel
            if target.is_dir():
                shutil.rmtree(target)
        log.info("Removed from the mirror: %s '%s' (%s) — recoverable from git history",
                 kind, item.get("title", ""), oid)


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
