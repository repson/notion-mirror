"""Command line interface: backup | restore | status."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import __version__
from .backup import run_backup
from .config import COMMENTS_ALWAYS, COMMENTS_OFF, COMMENTS_ON_CHANGE


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="notion-mirror",
        description="Local mirror of your Notion workspace in a git repository. "
                    "Run the backup, commit and push: git history is your version history.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_backup = sub.add_parser("backup", help="Sync the mirror with Notion")
    p_backup.add_argument("--token", help="Notion token (default: NOTION_TOKEN or .env)")
    p_backup.add_argument("--output", default=None,
                          help="Mirror directory (default: $NOTION_BACKUP_DIR or backup/)")
    p_backup.add_argument(
        "--comments", default=None,
        choices=[COMMENTS_ALWAYS, COMMENTS_ON_CHANGE, COMMENTS_OFF],
        help="Comments: always = fetch them on every run (they do not bump "
             "last_edited_time); on-change = only when the page changes (faster); "
             "off = do not back them up (default: always)",
    )
    p_backup.add_argument("--force", action="store_true", help="Re-download everything (full run)")
    p_backup.add_argument("--quiet", action="store_true", help="Less logging (warnings only)")

    p_restore = sub.add_parser(
        "restore", help="Restore a page from the backup into Notion (experimental)"
    )
    p_restore.add_argument("page", help="Path of the .md in the backup, or page id")
    p_restore.add_argument("--parent", required=True, help="Id of the parent page in Notion")
    p_restore.add_argument("--title", help="Title of the restored page (default: original)")
    p_restore.add_argument("--token", help="Notion token (default: NOTION_TOKEN or .env)")
    p_restore.add_argument("--output", default=None,
                           help="Mirror directory (default: $NOTION_BACKUP_DIR or backup/)")
    p_restore.add_argument("--dry-run", action="store_true",
                           help="Show the plan without writing anything to Notion")

    p_status = sub.add_parser("status", help="Summary of the last backup (no API calls)")
    p_status.add_argument("--output", default=None,
                          help="Mirror directory (default: $NOTION_BACKUP_DIR or backup/)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        stream=sys.stderr,
        level=logging.WARNING if getattr(args, "quiet", False) else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx/httpcore log every request at INFO level; too noisy for our output.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    from .config import from_args

    if args.command == "backup":
        return _run_command(run_backup, from_args(args))
    if args.command == "restore":
        from .restore import run_restore

        return _run_command(
            run_restore,
            lambda: run_restore(from_args(args), args.page, args.parent, args.title, args.dry_run),
        )
    if args.command == "status":
        return _status(from_args(args))
    parser.error("unknown command")
    return 2


def _run_command(fn, *args) -> int:
    """Runs a command, turning fatal API errors into clean messages."""
    from .http import NotionAPIError

    try:
        return fn(*args)
    except NotionAPIError as exc:
        log = logging.getLogger("notion-mirror")
        if exc.status == 401 or (exc.status == 403 and exc.code != "restricted_resource"):
            log.error("Access denied (%s): check that your token is valid and has "
                      "the Notion API capability. %s", exc.status, exc)
        else:
            log.error("The Notion API request failed: %s", exc)
        return 1


def _status(cfg) -> int:
    root = cfg.output
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        print(f"No backup found at {root}. Run: notion-mirror backup")
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pages = manifest.get("pages", {})
    data_sources = manifest.get("data_sources", {})
    counts = manifest.get("counts", {})
    print(f"Mirror at {root}")
    print(f"  Pages:     {len(pages)}")
    print(f"  Databases: {len(data_sources)}")
    print(f"  Files:     {counts.get('files', 0)}")
    rows = sum(e.get("row_count", 0) for e in data_sources.values())
    print(f"  Rows:      {rows}")

    print("\nTop level:")
    top = sorted(
        (e["path"], e.get("title", "")) for e in pages.values()
        if e["path"].count("/") == 1
    )
    for path, title in top:
        print(f"  {path}  ({title})")
    top_ds = sorted(
        (e["path"], e.get("title", "")) for e in data_sources.values()
        if e["path"].count("/") == 1
    )
    for path, title in top_ds:
        print(f"  {path}/  ({title}) [database]")

    report = root / "report.md"
    if report.exists():
        print("\n--- last report ---")
        print(report.read_text(encoding="utf-8").strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
