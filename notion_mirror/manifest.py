"""Reading/writing the manifest (``backup/manifest.json``) and the report.

The manifest is the mirror's index: what exists, where, and with which
timestamp. It is deliberately deterministic (no "generated_at": if nothing
changed, the file does not change and the commit comes out empty). The
run's date/time lives in ``report.md``, which is always rewritten.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

API_VERSION = "2026-03-11"


def load(root: Path) -> dict:
    path = root / "manifest.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def dumps(data: dict) -> str:
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def write_if_different(path: Path, text: str) -> bool:
    """Writes only if the content changes. Returns True if it wrote."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.exists() and path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    path.write_text(text, encoding="utf-8")
    return True


def build(inv, page_paths: dict, ds_paths: dict, page_files: dict, ds_files: dict,
          failed: dict | None = None) -> dict:
    """:param failed: {id: previous_last_edited_time} for pages/databases whose
        fetch failed: they keep the previous timestamp so the next run retries."""
    failed = failed or {}
    pages = {}
    for pid, page in inv.pages.items():
        pages[pid] = {
            "title": _title_of_page(page),
            "path": page_paths[pid],
            "parent_id": (page.get("parent") or {}).get(
                "page_id"
            ) or (page.get("parent") or {}).get("block_id")
            or (page.get("parent") or {}).get("data_source_id")
            or (page.get("parent") or {}).get("database_id"),
            "last_edited_time": failed.get(pid, page.get("last_edited_time")),
            "files": sorted(set(page_files.get(pid, []))),
        }
    data_sources = {}
    for dsid, ds in inv.data_sources.items():
        data_sources[dsid] = {
            "title": _title_of_ds(ds),
            "path": ds_paths[dsid],
            "database_id": (ds.get("parent") or {}).get("database_id"),
            "last_edited_time": failed.get(f"ds:{dsid}", ds.get("last_edited_time")),
            "row_count": len(inv.rows.get(dsid, [])),
            "files": sorted(set(ds_files.get(dsid, []))),
        }
    all_files: set[str] = set()
    for files in page_files.values():
        all_files.update(files)
    for files in ds_files.values():
        all_files.update(files)
    return {
        "api_version": API_VERSION,
        "pages": pages,
        "data_sources": data_sources,
        "counts": {
            "pages": len(pages),
            "data_sources": len(data_sources),
            "files": len(all_files),
        },
    }


def referenced_files(manifest: dict) -> set[str]:
    """Attachments referenced by the manifest's pages and databases."""
    refs: set[str] = set()
    for entry in (manifest.get("pages") or {}).values():
        refs.update(entry.get("files") or [])
    for entry in (manifest.get("data_sources") or {}).values():
        refs.update(entry.get("files") or [])
    return refs


def build_report(
    *,
    started_at: float,
    duration: float,
    requests: int,
    stats: dict,
    removed: list[dict],
    warnings: list[str],
    files_gc: int,
) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        f"# Backup report — {now}",
        "",
        f"- Duration: {duration:.0f}s · API requests: {requests}",
        f"- Pages: {stats.get('pages_total', 0)} "
        f"(new: {stats.get('pages_new', 0)}, modified: {stats.get('pages_changed', 0)}, "
        f"unchanged: {stats.get('pages_unchanged', 0)}, removed: {len(removed)})",
        f"- Databases: {stats.get('ds_total', 0)} (updated: {stats.get('ds_changed', 0)})",
        f"- Files: {stats.get('files_total', 0)} "
        f"(new: {stats.get('files_new', 0)}, orphaned removed: {files_gc})",
        "",
    ]
    if removed:
        lines.append("## Removed from Notion (kept in git history)")
        lines.extend(
            f"- [{r.get('kind')}] {r.get('title', '')} ({r.get('id')}) — {r.get('reason')}"
            for r in removed
        )
        lines.append("")
    if warnings:
        lines.append("## Warnings")
        lines.extend(f"- {w}" for w in warnings)
        lines.append("")
    lines.extend([
        "## Next step",
        "```bash",
        "git add -A",
        f'git commit -m "backup: {now}"',
        "git push",
        "```",
        "",
    ])
    return "\n".join(lines)


def _title_of_page(page: dict) -> str:
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return "".join(rt.get("plain_text", "") for rt in prop.get("title") or [])
    return ""


def _title_of_ds(ds: dict) -> str:
    return "".join(rt.get("plain_text", "") for rt in ds.get("title") or [])
