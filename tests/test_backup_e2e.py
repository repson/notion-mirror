"""End-to-end test of the orchestrator with a simulated API.

Validates the full mirror semantics:
- first run (layout, attachments, comments, manifest)
- second run without changes → zero re-downloads, identical manifest
- content edit on an existing page → only that page is re-downloaded
- new comment without a page edit → still detected
- page deletion → disappears from the mirror
- page move → git sees it as a rename
- stale search index → reconciliation keeps the page
- transient API failure → retried on the next run
"""

import json
from pathlib import Path

import pytest

import notion_mirror.backup as backup_mod
from notion_mirror.config import Config
from notion_mirror.http import NotionAPIError
from notion_mirror.model import is_trashed

A = "11111111-1111-1111-1111-111111111111"  # Projects (root)
B = "22222222-2222-2222-2222-222222222222"  # Meeting Notes (child of A)
C = "33333333-3333-3333-3333-333333333333"  # Journal (root)
R = "44444444-4444-4444-4444-444444444444"  # Task 1 (database row)
DB = "55555555-5555-5555-5555-555555555555"  # Database
DS = "66666666-6666-6666-6666-666666666666"  # Data source "Tasks"
USER = "77777777-7777-7777-7777-777777777777"

T = "2026-01-01T00:00:00.000Z"
T2 = "2026-01-02T00:00:00.000Z"

IMG_URL = "https://prod-files-secure.s3.us-west-2.amazonaws.com/WS/IMG1/logo.png?sig=1"
ROW_FILE_URL = "https://prod-files-secure.s3.us-west-2.amazonaws.com/WS/ROWF/doc.pdf?sig=2"


class FakeHTTP:
    downloads = 0

    def __init__(self, token, **kwargs):
        self.requests = 0

    def download_to(self, url, dest):
        FakeHTTP.downloads += 1
        Path(dest).write_bytes(b"FAKE:" + url.encode())
        content_type = "image/png" if ".png" in url else "application/pdf"
        return True, content_type

    def close(self):
        pass


class FakeApi:
    markdown_fetched: list[str] = []

    def __init__(self, http):
        self.http = http

    def search_objects(self, value):
        if value == "page":
            objects = [p for p in DATA["pages"].values() if not p.get("_hidden") and not is_trashed(p)]
        else:
            objects = [d for d in DATA["data_sources"].values() if not d.get("_hidden")]
        return sorted(objects, key=lambda o: (o.get("last_edited_time") or "", o["id"]))

    def get_page(self, page_id):
        return DATA["pages"].get(page_id)

    def get_page_markdown(self, page_id):
        if page_id in DATA.get("fail_markdown", set()):
            raise NotionAPIError(500, "internal_server_error", "transient boom")
        FakeApi.markdown_fetched.append(page_id)
        return {
            "object": "page_markdown", "id": page_id,
            "markdown": DATA["markdown"].get(page_id, ""),
            "truncated": False, "unknown_block_ids": [], "warnings": [],
        }

    def get_page_comments(self, page_id):
        return DATA["comments"].get(page_id, [])

    def get_block_children(self, block_id):
        return DATA["blocks"].get(block_id, [])

    def get_block(self, block_id):
        return None

    def get_database(self, database_id):
        return DATA["databases"].get(database_id)

    def get_data_source(self, data_source_id):
        return DATA["data_sources"].get(data_source_id)

    def query_data_source(self, data_source_id):
        return list(DATA["rows"].get(data_source_id, []))

    def get_user(self, user_id):
        return DATA["users"].get(user_id)

    def user_display(self, user):
        return (user or {}).get("name", "")


def make_page(pid, title, parent, properties=None):
    if properties is None:
        props = {"title": {"id": "t", "type": "title", "title": [{"plain_text": title}]}}
    else:
        props = properties
    return {
        "object": "page", "id": pid,
        "created_time": T, "last_edited_time": T2,
        "in_trash": False, "is_archived": False, "is_locked": False,
        "url": f"https://www.notion.so/{title.replace(' ', '-')}-{pid[:8]}",
        "public_url": None,
        "parent": parent,
        "properties": props,
        "icon": None, "cover": None,
        "created_by": {"object": "user", "id": USER},
        "last_edited_by": {"object": "user", "id": USER},
    }


def make_comment(cid, when, text):
    return {
        "object": "comment", "id": cid,
        "created_time": when, "last_edited_time": when,
        "created_by": {"object": "user", "id": USER},
        "parent": {"type": "page_id", "page_id": A},
        "discussion_id": "disc-1",
        "text": [{"plain_text": text}],
        "resolved": None,
    }


def build_data():
    row = make_page(
        R, "Task 1",
        {"type": "data_source_id", "data_source_id": DS, "database_id": DB},
        properties={
            # In the real API, row properties use the same names as the data
            # source schema (not "title").
            "Name": {"id": "t", "type": "title", "title": [{"plain_text": "Task 1"}]},
            "Status": {"id": "e", "type": "status", "status": {"name": "In progress"}},
            "Attachments": {"id": "f", "type": "files", "files": [
                {"name": "doc.pdf", "type": "file",
                 "file": {"url": ROW_FILE_URL, "expiry_time": "2026-11-01T20:00:00.000Z"}},
            ]},
        },
    )
    schema = {
        "Name": {"id": "t", "name": "Name", "description": None, "type": "title", "title": {}},
        "Status": {"id": "e", "name": "Status", "description": None, "type": "status",
                   "status": {"options": [{"name": "In progress", "color": "blue"}], "groups": []}},
        "Attachments": {"id": "f", "name": "Attachments", "description": None, "type": "files", "files": {}},
    }
    database = {
        "object": "database", "id": DB,
        "data_sources": [{"id": DS, "name": "Tasks"}],
        "title": [{"plain_text": "Tasks"}], "description": [],
        "created_time": T, "last_edited_time": T2,
        "created_by": {"object": "user", "id": USER},
        "last_edited_by": {"object": "user", "id": USER},
        "parent": {"type": "page_id", "page_id": A},
        "url": f"https://www.notion.so/{DB}", "in_trash": False,
        "is_inline": False, "archived": False, "icon": None, "cover": None,
        "public_url": None,
    }
    data_source = {
        "object": "data_source", "id": DS,
        "title": [{"plain_text": "Tasks"}], "description": [],
        "parent": {"type": "database_id", "database_id": DB},
        "database_parent": {"type": "page_id", "page_id": A},
        "is_inline": False, "database_type": None, "in_trash": False,
        "created_time": T, "last_edited_time": T2,
        "created_by": {"object": "user", "id": USER},
        "last_edited_by": {"object": "user", "id": USER},
        "properties": schema,
        "icon": None, "cover": None,
        "url": f"https://www.notion.so/{DS}", "public_url": None,
    }
    return {
        "pages": {
            A: make_page(A, "Projects", {"type": "workspace", "workspace": True}),
            B: make_page(B, "Meeting Notes", {"type": "page_id", "page_id": A}),
            C: make_page(C, "Journal", {"type": "workspace", "workspace": True}),
            R: row,
        },
        "markdown": {
            A: f"# Projects\n\n![logo]({IMG_URL})\n\nA [web](https://example.com) link\n",
            B: "Notes from the meeting...",
            C: "Dear journal...",
            R: "Details of task 1",
        },
        "comments": {
            A: [make_comment("c1", "2026-02-01T00:00:00.000Z", "First comment")],
        },
        "blocks": {
            A: [
                {"object": "block", "id": B, "type": "child_page",
                 "child_page": {"title": "Meeting Notes"},
                 "parent": {"type": "page_id", "page_id": A}},
                {"object": "block", "id": DB, "type": "child_database",
                 "child_database": {"title": "Tasks"},
                 "parent": {"type": "page_id", "page_id": A}},
            ],
        },
        "databases": {DB: database},
        "data_sources": {DS: data_source},
        "rows": {DS: [row]},
        "users": {USER: {"object": "user", "id": USER, "name": "Alice", "type": "person"}},
    }


DATA: dict = {}


@pytest.fixture
def data():
    global DATA
    DATA = build_data()
    FakeHTTP.downloads = 0
    FakeApi.markdown_fetched = []
    return DATA


def run(monkeypatch, tmp_path, comments="always", force=False) -> Path:
    cfg = Config(
        token="test-token",
        output=tmp_path / "backup",
        cache=tmp_path / ".cache",
        comments=comments,
        force=force,
    )
    monkeypatch.setattr(backup_mod, "NotionHTTP", FakeHTTP)
    monkeypatch.setattr(backup_mod, "NotionAPI", FakeApi)
    assert backup_mod.run_backup(cfg) == 0
    return cfg.output


# --------------------------------------------------------------------- tests


def test_first_run(monkeypatch, tmp_path, data):
    root = run(monkeypatch, tmp_path)

    a_md = root / "pages" / "projects-11111111.md"
    assert a_md.exists()
    content = a_md.read_text(encoding="utf-8")
    assert "/backup/files/" in content
    assert "prod-files-secure" not in content  # the signed URL is gone
    assert "https://example.com" in content    # the external link stays

    assert (root / "pages" / "projects-11111111" / "meeting-notes-22222222.md").exists()
    assert (root / "pages" / "journal-33333333.md").exists()

    ds_dir = root / "pages" / "projects-11111111" / "tasks-66666666"
    assert (ds_dir / "data_source.json").exists()
    assert (ds_dir / "rows.csv").exists()
    assert (ds_dir / "rows.json").exists()
    assert (ds_dir / "task-1-44444444.md").exists()
    assert "Task 1" in (ds_dir / "rows.csv").read_text(encoding="utf-8")

    sidecar = json.loads((root / "pages" / "projects-11111111.json").read_text(encoding="utf-8"))
    assert sidecar["comments"][0]["text"] == "First comment"
    assert sidecar["comments"][0]["created_by"]["name"] == "Alice"
    assert len(sidecar["files"]) == 1

    names = {f.name for f in (root / "files").glob("*/*")}
    assert names == {"logo.png", "doc.pdf"}

    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["pages"]) == 4
    assert len(manifest["data_sources"]) == 1
    assert manifest["counts"]["files"] == 2
    assert (root / "report.md").exists()


def test_second_run_without_changes(monkeypatch, tmp_path, data):
    root = run(monkeypatch, tmp_path)
    first = (root / "manifest.json").read_bytes()

    FakeHTTP.downloads = 0
    FakeApi.markdown_fetched = []
    run(monkeypatch, tmp_path)

    assert (root / "manifest.json").read_bytes() == first
    assert FakeHTTP.downloads == 0
    assert FakeApi.markdown_fetched == []


def test_content_edit_and_new_comment(monkeypatch, tmp_path, data):
    root = run(monkeypatch, tmp_path)
    a_md = root / "pages" / "projects-11111111.md"
    a_bytes = a_md.read_bytes()

    # New content in C (with its last_edited_time).
    data["pages"][C]["last_edited_time"] = "2026-03-01T00:00:00.000Z"
    data["markdown"][C] = "Dear journal... a new edit"
    # New comment on A WITHOUT touching last_edited_time.
    data["comments"][A].append(make_comment("c2", "2026-03-05T00:00:00.000Z", "Second comment"))

    FakeApi.markdown_fetched = []
    run(monkeypatch, tmp_path)

    assert "a new edit" in (root / "pages" / "journal-33333333.md").read_text(encoding="utf-8")
    assert FakeApi.markdown_fetched == [C]
    assert a_md.read_bytes() == a_bytes  # A's .md is not re-downloaded
    sidecar = json.loads(a_md.with_suffix(".json").read_text(encoding="utf-8"))
    assert len(sidecar["comments"]) == 2


def test_page_deletion(monkeypatch, tmp_path, data):
    root = run(monkeypatch, tmp_path)
    b_md = root / "pages" / "projects-11111111" / "meeting-notes-22222222.md"
    b_json = b_md.with_suffix(".json")
    assert b_md.exists()

    data["pages"][B]["in_trash"] = True
    run(monkeypatch, tmp_path)

    assert not b_md.exists()
    assert not b_json.exists()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert B not in manifest["pages"]
    assert len(manifest["pages"]) == 3
    # The logo is still referenced by A: it is not deleted.
    assert list((root / "files").glob("*/logo.png"))


def test_page_move(monkeypatch, tmp_path, data):
    root = run(monkeypatch, tmp_path)
    old = root / "pages" / "journal-33333333.md"
    assert old.exists()

    data["pages"][C]["parent"] = {"type": "page_id", "page_id": A}
    FakeApi.markdown_fetched = []
    run(monkeypatch, tmp_path)

    new = root / "pages" / "projects-11111111" / "journal-33333333.md"
    assert new.exists()
    assert not old.exists()
    assert FakeApi.markdown_fetched == []  # moving does not re-download content
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["pages"][C]["path"] == "pages/projects-11111111/journal-33333333"


def test_stale_search_index(monkeypatch, tmp_path, data):
    root = run(monkeypatch, tmp_path)
    # The search index does not return C, but the page is still alive.
    data["pages"][C]["_hidden"] = True
    run(monkeypatch, tmp_path)

    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert C in manifest["pages"]
    assert (root / "pages" / "journal-33333333.md").exists()


def test_transient_failure_and_recovery(monkeypatch, tmp_path, data):
    root = run(monkeypatch, tmp_path)
    a_md = root / "pages" / "projects-11111111.md"
    original = a_md.read_text(encoding="utf-8")

    data["pages"][A]["last_edited_time"] = "2026-04-01T00:00:00.000Z"
    data["markdown"][A] = "# Projects NEW"
    data["fail_markdown"] = {A}
    run(monkeypatch, tmp_path)  # fails with a warning, without breaking the run

    assert a_md.read_text(encoding="utf-8") == original
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["pages"][A]["last_edited_time"] == T2  # keeps the previous one

    data["fail_markdown"] = set()
    run(monkeypatch, tmp_path)
    assert "NEW" in a_md.read_text(encoding="utf-8")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["pages"][A]["last_edited_time"] == "2026-04-01T00:00:00.000Z"


def test_comments_off(monkeypatch, tmp_path, data):
    root = run(monkeypatch, tmp_path, comments="off")
    sidecar_path = root / "pages" / "projects-11111111.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert "comments" not in sidecar
    first = sidecar_path.read_bytes()

    data["comments"][A].append(make_comment("c2", "2026-03-05T00:00:00.000Z", "Another one"))
    run(monkeypatch, tmp_path, comments="off")
    assert sidecar_path.read_bytes() == first  # with off, comments are not fetched
