import json

from notion_mirror.model import Inventory
from notion_mirror.sidecar import (
    build_page_sidecar,
    sanitize_comments,
    sanitize_node,
)
from notion_mirror.urls import guess_filename


class FakeStore:
    """A fake store: stable local paths per URL (deterministic)."""

    def __init__(self):
        self.urls: list[str] = []

    def ensure(self, url, sink=None):
        if url not in self.urls:
            self.urls.append(url)
        local = f"files/{self.urls.index(url) + 1:012x}/{guess_filename(url)}"
        if sink is not None:
            sink.append(local)
        return local


def users_fn(user_id):
    return {"id": user_id, "name": "Alice"}


def make_page():
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "object": "page",
        "url": "https://www.notion.so/Page-11111111",
        "created_time": "2026-01-01T00:00:00.000Z",
        "last_edited_time": "2026-02-01T00:00:00.000Z",
        "created_by": {"object": "user", "id": "u1"},
        "last_edited_by": {"object": "user", "id": "u1"},
        "parent": {"type": "page_id", "page_id": "2222"},
        "icon": {"type": "emoji", "emoji": "📌"},
        "cover": {
            "type": "file",
            "file": {
                "url": "https://prod-files-secure.s3.us-west-2.amazonaws.com/WS/C/cover.png?sig=1",
                "expiry_time": "2026-11-01T13:00:00.000Z",
            },
        },
        "in_trash": False,
        "properties": {
            "title": {"type": "title", "title": [{"plain_text": "My page"}]},
            "Files": {
                "type": "files",
                "files": [
                    {"type": "file", "file": {
                        "url": "https://prod-files-secure.s3.us-west-2.amazonaws.com/WS/F/doc.pdf?sig=2",
                        "name": "doc.pdf",
                    }},
                    {"type": "external", "external": {
                        "url": "https://example.com/imagen.png",
                    }},
                ],
            },
            "People": {"type": "people", "people": [{"id": "u1"}]},
            "Relation": {"type": "relation", "relation": [{"id": "3333"}]},
            "Date": {"type": "date", "date": {"start": "2026-01-01", "end": None}},
        },
    }


def test_sidecar_sanitizes_and_resolves():
    inv = Inventory()
    inv.pages["3333"] = {
        "id": "3333", "object": "page",
        "properties": {"title": {"type": "title", "title": [{"plain_text": "Related"}]}},
    }
    store = FakeStore()
    sink: list = []
    sidecar = build_page_sidecar(make_page(), inv, users_fn, store, [], sink)

    assert sidecar["title"] == "My page"
    assert sidecar["created_by"] == {"id": "u1", "name": "Alice"}
    # Emoji icon intact; cover downloaded and without the volatile expiry_time.
    assert sidecar["icon"] == {"type": "emoji", "emoji": "📌"}
    assert sidecar["cover"]["local_path"].startswith("files/")
    assert "expiry_time" not in json.dumps(sidecar)
    # Properties.
    files = sidecar["properties"]["Files"]["files"]
    assert files[0]["local_path"].endswith("doc.pdf")
    assert files[0]["url"].endswith("doc.pdf") and "sig" not in files[0]["url"]
    assert files[1]["local_path"].startswith("files/")
    assert sidecar["properties"]["People"]["people"] == [{"id": "u1", "name": "Alice"}]
    assert sidecar["properties"]["Relation"]["relation"] == [{"id": "3333", "title": "Related"}]
    # The sink holds every attachment (cover + 2 property files).
    assert len(sink) == 3
    assert sidecar["files"] == sorted(set(sink))


def test_sidecar_is_deterministic():
    inv = Inventory()
    store = FakeStore()
    one = build_page_sidecar(make_page(), inv, users_fn, store, [], [])
    two = build_page_sidecar(make_page(), inv, users_fn, store, [], [])
    assert json.dumps(one, sort_keys=True) == json.dumps(two, sort_keys=True)


def test_sanitize_node_leaves_plain_rich_text_alone():
    store = FakeStore()
    node = {"type": "paragraph", "rich_text": [{"plain_text": "hello", "href": None}]}
    assert sanitize_node(node, store, None) == node


def test_sanitize_node_with_nested_file():
    store = FakeStore()
    sink: list = []
    node = {"caption": [], "type": "file", "file": {
        "url": "https://prod-files-secure.s3.us-west-2.amazonaws.com/WS/X/a.png?sig=9"
    }}
    out = sanitize_node(node, store, sink)
    # The result is the flat local-reference form (no expiry_time).
    assert out["local_path"].startswith("files/")
    assert "sig" not in out["url"]
    assert out["name"] == "a.png"


def test_sanitize_comments_is_sorted():
    comments = [
        {"id": "c2", "created_time": "2026-02-02T00:00:00.000Z", "created_by": {"id": "u1"},
         "text": [{"plain_text": "second"}], "resolved": None},
        {"id": "c1", "created_time": "2026-01-02T00:00:00.000Z", "created_by": {"id": "u1"},
         "text": [{"plain_text": "first"}], "resolved": None},
    ]
    out = sanitize_comments(comments, users_fn, FakeStore(), [])
    assert [c["id"] for c in out] == ["c1", "c2"]
    assert out[0]["created_by"]["name"] == "Alice"
