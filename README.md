# notion-mirror

> **This repository (`notion-mirror`) is my private mirror**: it contains the tool's source code
> **and** the actual backup of my Notion workspace in `backup/`. The tool itself is published separately
> for the community (see public repo TBD).

Local mirror of your [Notion](https://notion.so) workspace in a git repository.

- **All your content** — pages, databases (schema and rows), attachments, icons, covers and comments — is downloaded into `backup/` as Markdown + JSON + binary files.
- **The repository is a mirror**: whatever is deleted in Notion (verified against the API) is removed from the working tree; the last copy stays in git history and is recovered with `git checkout`.
- **Version history for free**: you run the backup, commit and push. Unchanged pages produce identical bytes, so every commit only contains real changes — and `git log -p backup/pages/my-page.md` is the history of that document.

```
┌────────────┐   notion-mirror backup    ┌─────────────────────────────┐
│   Notion   │ ─────────────────────────▶ │ backup/  (mirror, in git)  │
└────────────┘                            └─────────────────────────────┘
                                                ▲ git add/commit/push
```

## Backup layout

```
backup/
├── manifest.json               # index: ids, paths, timestamps, attachments
├── report.md                   # report of the last run
├── pages/                      # mirrors the Notion tree
│   ├── projects-a1b2c3d4.md    # page content (Notion-flavored Markdown)
│   ├── projects-a1b2c3d4.json  # sidecar: properties, icon, comments…
│   └── projects-a1b2c3d4/      # child pages
│       ├── meeting-notes-e5f6a7b8.md
│       └── tasks-c8d9e0f1/     # database (data source)
│           ├── data_source.json    # database metadata + schema
│           ├── rows.csv            # flat rows (importable into Notion)
│           ├── rows.json           # full-fidelity rows
│           └── task-1-11223344.md  # each row is also a page
└── files/                      # attachments, deduplicated by content hash
    └── 2f7a3b…/logo.png        # referenced from Markdown as /backup/files/…
```

- Notion attachment URLs expire in ~1h; the backup rewrites them into local paths, so images render when browsing the repo on GitHub, Obsidian or VS Code.
- Each page is named `slug-of-the-title + short id`: renaming a page is a rename for git, not a delete + create.
- Orphaned files (referenced by no page anymore) are removed from the mirror.

## Installation

Requires Python 3.10+.

```bash
git clone git@github.com:repson/notion-mirror.git && cd notion-mirror

# Create the virtual environment and install
uv venv && uv pip install -e .        # or: python -m venv .venv && pip install -e .

# Activate the environment (you need to do this in every shell session)
source .venv/bin/activate             # Linux / macOS
# .venv\Scripts\activate              # Windows PowerShell

cp .env.example .env                  # and paste your token
```

The `notion-mirror` command is only available inside the virtual environment.
If you prefer not to activate it, use the full path:
```bash
.venv/bin/notion-mirror backup        # or: python -m notion_mirror backup
```

## Configuration

Copy `.env.example` to `.env` (gitignored) and set your token:

```bash
cp .env.example .env
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `NOTION_TOKEN` | **yes** | — | Notion token (a Personal Access Token is recommended). |
| `NOTION_BACKUP_DIR` | no | `backup` | Mirror directory. |
| `NOTION_COMMENTS` | no | `always` | Comment policy: `always`, `on-change` or `off`. |
| `NOTION_REQUESTS_PER_SECOND` | no | `2.5` | Request pacing against the API. |

Precedence: CLI flags > environment variables > `.env` file > defaults.

### Token: Personal Access Token (recommended)

Create a **Personal Access Token (PAT)** at <https://www.notion.so/developers/tokens> with the **Notion API** capability:

- A PAT acts with **your user permissions**: it sees your whole workspace without sharing pages with an integration.
- On the Free plan only *workspace owners* can create them (in a personal workspace, that's you).
- **They expire after one year at most**: when it does, create a new one, update `.env` and move on. Put a reminder in your calendar.

Alternative: a classic internal integration (`notion.so/my-integrations`), but you must manually share every top-level page with it, and it only sees what was shared.

## Usage

Make sure the virtual environment is activated (`source .venv/bin/activate`) or use the full path
(`.venv/bin/notion-mirror`).

```bash
# Sync the mirror with Notion
notion-mirror backup

# See what is backed up
notion-mirror status

# Commit and push (the daily flow)
git add -A && git commit -m "backup: $(date +%F)" && git push

# Force a full re-download (recommended ~monthly, see below)
notion-mirror backup --force
```

`backup` options:

| Option | Description |
|---|---|
| `--comments always\|on-change\|off` | Comments do not bump a page's `last_edited_time`, so by default they are fetched **on every run** (one request per page). `on-change` fetches them only when the page changed: faster, but it may miss comments on unedited pages. |
| `--force` | Re-download everything. |
| `--output DIR` | Mirror directory (default: `backup/`). |
| `--quiet` | Less logging. |

**Automation**: the command is a plain CLI; it works the same from cron, a systemd timer or n8n (an *Execute Command* node). For cron, use the full path to the binary so you don't need to activate the virtual environment. Example — daily at 08:00:

```
0 8 * * *  cd /path/to/repo && .venv/bin/notion-mirror backup --quiet && git add -A && git commit -m "backup: $(date +\%F)" && git push
```

## Recovery

### Recover a deleted page or an old version

```bash
# What did "meeting-notes" say yesterday?
git log --oneline -- "backup/pages/**/meeting-notes-*"
git checkout <commit> -- "backup/pages/projects-a1b2c3d4/meeting-notes-e5f6a7b8.md"
```

The recovered file is plain Markdown: copy it back into Notion by hand, import it from Notion (*Settings → Import → Markdown*), or use the restore command.

### Restore a page into Notion (experimental)

```bash
notion-mirror restore backup/pages/journal-33333333.md --parent <parent-page-id>
# or by page id:
notion-mirror restore 33333333-3333-3333-3333-333333333333 --parent <parent-page-id>
```

Creates the page under the given parent, writes the content with the official Markdown endpoint and re-uploads the attachments as blocks. Limitations: it does not restore database row properties, sub-pages (restore them one by one), or blocks Notion Markdown does not support. `--dry-run` shows the plan without writing anything.

### Re-importing the whole workspace

The Markdown+CSV layout is compatible with Notion's importer (*Settings → Import → Markdown & CSV*). For full fidelity, Notion's native export (*Settings → Export all workspace content*) remains the reference: there is no full-export API outside Enterprise.

## How it works

1. **Full enumeration on every run**: paginated search + a recursive tree walk (children of every new/edited/root page). Notion's search index is not a complete inventory; that's why it is combined with the walk.
2. **Reconciliation**: whatever was in the previous manifest but no longer shows up is verified one by one against the API. Confirmed deleted (404 / trash) → removed from the mirror. Still alive → kept with a warning (stale index).
3. **Incremental**: only pages whose `last_edited_time` changed are re-downloaded. Moves and renames are detected by recomputing paths, without re-downloading.
4. **API limits**: ~3 requests/second on personal plans. The client paces itself (2.5 req/s by default, see `NOTION_REQUESTS_PER_SECOND`), retries 429/529 honoring `Retry-After`, and uses backoff for 5xx on idempotent requests. A ~1000-page workspace takes ~10 min the first time and much less afterwards.
5. **No internal API dependencies**: only the documented public API (version 2026-03-11) with a PAT. No `token_v2` cookies.

## Known limitations

- **Trash**: the API cannot read the trash; deleted pages vanish from the mirror (the previous copy stays in git). Deletions are detected on the next run.
- **Resolved comments**: the comments endpoint returns unresolved threads.
- **Database views** (filters, sorts, groupings) are not backed up; the schema and all rows are.
- **Blocks Notion Markdown does not support** (embeds, bookmarks, link previews) show up as `<unknown …/>`; the subtree, if accessible, is fetched separately and problematic blocks are stored as raw JSON next to the page (`.blocks.json`).
- **External images in page content** (hosted outside Notion): the original link is kept; attachments uploaded to Notion are downloaded.
- **Notion Sync** (databases synced from Jira/Salesforce) is backed up as regular data sources.

## Security

- The token lives in `.env` (gitignored). Create a PAT with **only the Notion API capability**.
- The repository contains all your knowledge: keep it **private** on GitHub.
- If the content is sensitive, encrypt the repo (`git-crypt`, `age`) or the `backup/` directory.
- Binary attachments make the repo grow; if it ever gets too heavy, migrate `backup/files/` to [Git LFS](https://git-lfs.com/) without changing the format.

## Development

```bash
uv pip install -e ".[dev]"
python -m pytest          # full suite (offline: the API is simulated)
```

Code layout in `notion_mirror/`: `http.py` (paced client with retries), `api.py` (endpoints), `inventory.py` (enumeration and reconciliation), `tree.py` (mirror paths), `backup.py` (orchestrator), `sidecar.py`/`manifest.py` (deterministic output), `restore.py` (recovery), `files.py` (attachments with dedupe).

Utilities in `scripts/`: `fix_backup_format.py` — one-off migration that applies the current Markdown post-processing to `.md` files already in `backup/` (run it manually after changing `markdown.py`; normal backups post-process automatically as they download).
