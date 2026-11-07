from notion_mirror.model import Inventory
from notion_mirror.tree import ORPHAN_DIR, build_paths


def pid(char: str) -> str:
    return f"{char * 8}-{char * 4}-{char * 4}-{char * 4}-{char * 12}"


def page(id_: str, title: str, parent: dict) -> dict:
    return {
        "object": "page", "id": id_,
        "properties": {"title": {"type": "title", "title": [{"plain_text": title}]}},
        "parent": parent,
        "last_edited_time": "2026-01-01T00:00:00.000Z",
    }


def make_inventory() -> Inventory:
    inv = Inventory()
    a, b, r = pid("1"), pid("2"), pid("4")
    ds, db = pid("6"), pid("5")
    inv.pages[a] = page(a, "Parent", {"type": "workspace", "workspace": True})
    inv.pages[b] = page(b, "Child", {"type": "page_id", "page_id": a})
    inv.pages[r] = page(r, "Row", {"type": "data_source_id", "data_source_id": ds, "database_id": db})
    inv.data_sources[ds] = {
        "id": ds, "object": "data_source",
        "title": [{"plain_text": "Base"}],
        "parent": {"type": "database_id", "database_id": db},
        "database_parent": {"type": "page_id", "page_id": a},
    }
    inv.databases[db] = {
        "id": db, "object": "database", "title": [{"plain_text": "Base"}],
        "parent": {"type": "page_id", "page_id": a},
        "data_sources": [{"id": ds, "name": "Base"}],
    }
    return inv


def test_full_hierarchy():
    inv = make_inventory()
    pages, dss, warnings = build_paths(inv)

    a, b, r = pid("1"), pid("2"), pid("4")
    ds = pid("6")
    assert pages[a] == f"pages/parent-{a[:8]}"
    assert pages[b] == f"pages/parent-{a[:8]}/child-{b[:8]}"
    assert dss[ds] == f"pages/parent-{a[:8]}/base-{ds[:8]}"
    assert pages[r] == f"pages/parent-{a[:8]}/base-{ds[:8]}/row-{r[:8]}"
    assert warnings == []


def test_unknown_block_parent_goes_to_orphan():
    inv = make_inventory()
    x = pid("9")
    inv.pages[x] = page(x, "Loose", {"type": "block_id", "block_id": pid("f")})
    pages, _, warnings = build_paths(inv, resolve_block=lambda _bid: None)
    assert pages[x].startswith(ORPHAN_DIR)
    assert warnings


def test_block_parent_that_is_a_page():
    inv = make_inventory()
    x = pid("9")
    inv.pages[x] = page(x, "Sub", {"type": "block_id", "block_id": pid("1")})
    pages, _, _ = build_paths(inv)
    # A child_page block has the same id as the containing page.
    assert pages[x] == f"pages/parent-{pid('1')[:8]}/sub-{x[:8]}"


def test_cycle_does_not_hang():
    inv = Inventory()
    m, n = pid("a"), pid("b")
    inv.pages[m] = page(m, "M", {"type": "page_id", "page_id": n})
    inv.pages[n] = page(n, "N", {"type": "page_id", "page_id": m})
    pages, _, warnings = build_paths(inv)
    assert pages[m].startswith(ORPHAN_DIR) or pages[n].startswith(ORPHAN_DIR)
    assert warnings


def test_data_source_at_root():
    inv = Inventory()
    ds, db = pid("6"), pid("5")
    inv.data_sources[ds] = {
        "id": ds, "object": "data_source", "title": [{"plain_text": "Root DB"}],
        "parent": {"type": "database_id", "database_id": db},
        "database_parent": {"type": "workspace", "workspace": True},
    }
    inv.databases[db] = {
        "id": db, "object": "database", "title": [{"plain_text": "Root DB"}],
        "parent": {"type": "workspace", "workspace": True},
        "data_sources": [{"id": ds, "name": "Root DB"}],
    }
    _, dss, _ = build_paths(inv)
    assert dss[ds] == f"pages/root-db-{ds[:8]}"
