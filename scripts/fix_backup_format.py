#!/usr/bin/env python3
"""Post-processes existing backup .md files to fix Notion-specific formatting.

Applies the same transformations as post_process() in markdown.py:
  1. Prepends # Title to every page
  2. Converts <callout> blocks to > blockquotes
  3. Removes <table-of-contents /> placeholder
  4. Converts <page url="..."> tags to [Title](relative-path.md) links
  5. Converts <database ...> tags to [🗄 Title](dir/) links

Run this once after updating notion-mirror; subsequent backups produce
the corrected format automatically.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Add the project to sys.path
sys.path.insert(0, str(PROJECT_ROOT))

from notion_mirror.markdown import post_process


def load_manifest(root: Path) -> dict:
    path = root / "manifest.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def build_page_path_index(manifest: dict) -> dict[str, str]:
    """Builds a mapping: page UUID → backup-relative path (without .md)."""
    index: dict[str, str] = {}
    for pid, entry in (manifest.get("pages") or {}).items():
        path = entry.get("path")
        if path:
            index[pid] = path
    return index


def build_ds_path_index(manifest: dict) -> dict[str, str]:
    """Builds a mapping: data source UUID → backup-relative directory path."""
    index: dict[str, str] = {}
    for dsid, entry in (manifest.get("data_sources") or {}).items():
        path = entry.get("path")
        if path:
            index[dsid] = path
    return index


def fix_markdown_files(root: Path, page_index: dict, ds_index: dict) -> tuple[int, int]:
    """Reads every .md in backup/pages, post-processes it, and writes back.

    Returns (files_checked, files_changed).
    """
    pages_dir = root / "pages"
    if not pages_dir.is_dir():
        print(f"No pages directory at {pages_dir}")
        return 0, 0

    checked = 0
    changed = 0
    for md_path in sorted(pages_dir.rglob("*.md")):
        checked += 1
        original = md_path.read_text(encoding="utf-8")

        # Load the title from the sidecar
        sc_path = md_path.with_suffix(".json")
        title = ""
        if sc_path.exists():
            try:
                sidecar = json.loads(sc_path.read_text(encoding="utf-8"))
                title = sidecar.get("title", "")
            except (json.JSONDecodeError, OSError):
                pass

        # Skip if the file already has a proper title heading and no
        # Notion-specific tags (quick heuristic: look for <page, <database,
        # <callout, <table-of-contents).
        has_notion_tags = any(
            tag in original
            for tag in ("<page ", "<database ", "<callout", "<table-of-contents")
        )
        if not has_notion_tags and (not title or original.startswith(f"# {title}")):
            continue

        processed = post_process(
            original,
            title=title,
            page_url_to_path=page_index.get,
            ds_url_to_path=ds_index.get,
        )
        if not processed.endswith("\n"):
            processed += "\n"

        if processed != original:
            md_path.write_text(processed, encoding="utf-8")
            changed += 1
            print(f"  Fixed: {md_path.relative_to(root)}")

    return checked, changed


def main() -> int:
    root = PROJECT_ROOT / "backup"
    if not root.is_dir():
        print(f"backup/ directory not found at {root}.", file=sys.stderr)
        return 1

    manifest = load_manifest(root)
    if not manifest:
        print("No manifest.json found in backup/. Run the backup first.", file=sys.stderr)
        return 1

    page_index = build_page_path_index(manifest)
    ds_index = build_ds_path_index(manifest)

    print(f"Loaded manifest: {len(page_index)} pages, {len(ds_index)} databases")
    print("Post-processing .md files...")
    checked, changed = fix_markdown_files(root, page_index, ds_index)

    print(f"\nDone: {checked} files checked, {changed} changed.")
    if changed > 0:
        print("Next: git add -A && git commit -m 'fix: apply markdown post-processing'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())