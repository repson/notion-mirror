"""Restoring backup pages back into Notion (experimental).

Recreates a page from its ``.md`` + sidecar: creates the page under the given
parent, writes the content with the official Markdown endpoint
(``replace_content``) and re-uploads the attachments through the file uploads
API, appending them as blocks.

Known limitations (see README): database row properties, nested sub-pages
and blocks Notion Markdown does not support are not restored. Each child
page is restored individually with the same command.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
from pathlib import Path

from .api import NotionAPI
from .config import Config
from .http import NotionAPIError, NotionHTTP
from .manifest import load as load_manifest

log = logging.getLogger("notion-mirror")


def run_restore(cfg: Config, page_arg: str, parent_id: str,
                title_override: str | None, dry_run: bool) -> int:
    if not cfg.token:
        log.error("Missing token: use --token, the NOTION_TOKEN environment variable or the .env file")
        return 1

    root = cfg.output
    md_path, sidecar = _locate(root, page_arg)
    if md_path is None:
        log.error("Cannot find page '%s' in the backup %s", page_arg, root)
        return 1

    title = title_override or (sidecar or {}).get("title") or md_path.stem
    markdown = md_path.read_text(encoding="utf-8")

    # Local attachments referenced by the Markdown.
    from .markdown import local_file_links

    attachments: list[tuple[Path, str]] = []
    for local, _match in local_file_links(markdown):
        rel = local.lstrip("/")
        candidates = [root / rel, root.parent / rel]
        path = next((c for c in candidates if c.exists()), None)
        if path is None:
            log.warning("Attachment from the Markdown not found on disk: %s", local)
            continue
        attachments.append((path, local))

    log.info("Page to restore: '%s'", title)
    log.info("  Target parent: %s", parent_id)
    log.info("  Attachments to upload: %d", len(attachments))
    if dry_run:
        log.info("DRY-RUN: nothing will be written to Notion")
        return 0

    http = NotionHTTP(cfg.token, requests_per_second=cfg.requests_per_second)
    api = NotionAPI(http)
    try:
        icon = None
        if sidecar and (sidecar.get("icon") or {}).get("type") == "emoji":
            icon = {"type": "emoji", "emoji": sidecar["icon"]["emoji"]}

        page = api.create_page(parent_id, title, icon=icon)
        new_id = page["id"]
        log.info("Page created: %s", page.get("url"))

        # 1) Content (without local links: attachments come back as blocks).
        cleaned = _strip_local_links(markdown)
        try:
            api.replace_page_markdown(new_id, cleaned)
            log.info("Content restored (%d characters)", len(cleaned))
        except NotionAPIError as exc:
            log.error("Could not write the content: %s", exc)
            return 1

        # 2) Attachments: upload and add as blocks.
        for path, local in attachments:
            try:
                content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                upload = api.create_file_upload(path.name, content_type)
                api.send_file(upload["id"], path, path.name, content_type)
                block_type = "file"
                if content_type.startswith("image/"):
                    block_type = "image"
                elif content_type.startswith("video/"):
                    block_type = "video"
                elif content_type.startswith("audio/"):
                    block_type = "audio"
                api.append_blocks(new_id, [{
                    "object": "block",
                    "type": block_type,
                    block_type: {
                        "type": "file_upload",
                        "file_upload": {"id": upload["id"]},
                    },
                }])
                log.info("Attachment uploaded: %s", path.name)
            except NotionAPIError as exc:
                log.warning("Could not upload %s: %s (it remains available in the backup)",
                            path.name, exc)

        print(f"\nRestored: {page.get('url')}")
        return 0
    finally:
        http.close()


def _locate(root: Path, page_arg: str) -> tuple[Path | None, dict | None]:
    """Resolves the argument to (md path, sidecar) using the manifest."""
    md_path: Path | None = None
    if page_arg.endswith(".md"):
        candidate = Path(page_arg)
        md_path = candidate if candidate.exists() else root / page_arg
    else:
        manifest = load_manifest(root)
        entry = (manifest.get("pages") or {}).get(page_arg)
        if entry and entry.get("path"):
            md_path = root / (entry["path"] + ".md")
    if md_path is None or not md_path.exists():
        return None, None
    sidecar_path = md_path.with_suffix(".json")
    sidecar = None
    if sidecar_path.exists():
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    return md_path, sidecar


def _strip_local_links(markdown: str) -> str:
    """Removes local attachment links (they are restored as blocks)."""
    from .markdown import local_file_links

    result = markdown
    for _local, match in local_file_links(markdown):
        alt_match = re.match(r"(!?)\[([^\]\[]*)\]\(", match)
        image = bool(alt_match and alt_match.group(1) == "!")
        alt = alt_match.group(2) if alt_match else ""
        # The match starts at '(': find the beginning of the whole link.
        start = result.find(match)
        if start < 0:
            continue
        prefix_start = result.rfind("[", 0, start + 1)
        bang_start = prefix_start - 1 if prefix_start > 0 and result[prefix_start - 1] == "!" else prefix_start
        end = start + len(match)
        replacement = "" if image else alt
        result = result[:bang_start] + replacement + result[end:]
    return result
