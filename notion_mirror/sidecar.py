"""JSON sidecars: page and database metadata without volatile data.

Key rules so git works as a document history:
- Determinism: same properties → same bytes (``sort_keys``, 2-space indent).
- No signed URLs: they expire in ~1h and would pollute every diff. They are
  replaced by the local path of the downloaded attachment; the unsigned URL
  remains as the identity.
- Users as resolved ``{id, name}``, ISO dates as-is.
"""

from __future__ import annotations

from .model import data_source_title, database_title, page_title, parent_id_of
from .urls import guess_filename, url_identity


def sanitize_file_object(obj: dict, file_store, sink: list | None) -> dict:
    """Turns a Notion file/external object into a local reference.

    Input shape:
    ``{"type": "file", "file": {"url": ..., "expiry_time": ..., "name": ...}}`` or
    ``{"type": "external", "external": {"url": ...}}``
    """
    ftype = obj.get("type")
    inner = obj.get(ftype) if ftype in ("file", "external") else None
    if not isinstance(inner, dict) or not inner.get("url"):
        return obj
    url = inner["url"]
    if not url.startswith(("http://", "https://")):
        return {"type": ftype, "url": url}

    local = file_store.ensure(url, sink)
    out = {
        "type": ftype,
        "name": inner.get("name") or obj.get("name") or guess_filename(url),
        "url": url_identity(url),
    }
    if local:
        out["local_path"] = local
    else:
        out["download_failed"] = True
    return out


def sanitize_node(node, file_store, sink: list | None):
    """Recursively sanitizes any JSON value: file/external objects and nested
    lists/dicts (rich text with mentions, properties, etc.)."""
    if isinstance(node, list):
        return [sanitize_node(item, file_store, sink) for item in node]
    if isinstance(node, dict):
        ftype = node.get("type")
        if ftype in ("file", "external") and isinstance(node.get(ftype), dict):
            inner = node[ftype]
            if isinstance(inner, dict) and inner.get("url"):
                return sanitize_file_object(node, file_store, sink)
        return {key: sanitize_node(value, file_store, sink) for key, value in node.items()}
    return node


def _sanitize_icon_or_cover(obj: dict | None, file_store, sink: list | None):
    if not obj:
        return None
    ftype = obj.get("type")
    if ftype in ("file", "external"):
        return sanitize_file_object(obj, file_store, sink)
    if ftype == "custom_emoji":
        emoji = dict(obj.get("custom_emoji") or {})
        if emoji.get("url"):
            emoji = sanitize_node(emoji, file_store, sink)
        return {"type": "custom_emoji", "custom_emoji": emoji}
    # emoji / icon (noticon): no URLs, as-is.
    return obj


def _sanitize_people(people: list, users_fn) -> list:
    out = []
    for person in people or []:
        if isinstance(person, dict) and person.get("id"):
            user = users_fn(person["id"])
            name = (user or {}).get("name") if user else None
            out.append({"id": person["id"], "name": name or person.get("name") or person["id"]})
        else:
            out.append(person)
    return out


def _user_name(users_fn, user_id: str | None) -> str | None:
    """A readable name for a user id via ``users_fn`` (which returns the user
    object or None), tolerant of implementations returning plain text."""
    if not user_id:
        return None
    user = users_fn(user_id)
    if isinstance(user, str):
        return user or user_id
    if isinstance(user, dict):
        return user.get("name") or user_id
    return user_id


def _sanitize_properties(properties: dict, inv, file_store, users_fn, sink):
    """Page properties: files → local paths, people → names,
    relation → titles resolved through the inventory (when known)."""
    out = {}
    for name, prop in (properties or {}).items():
        ptype = prop.get("type")
        if ptype == "files":
            out[name] = {"type": "files", "files": [
                sanitize_file_object(f, file_store, sink) for f in prop.get("files") or []
            ]}
        elif ptype == "people":
            out[name] = {"type": "people", "people": _sanitize_people(prop.get("people"), users_fn)}
        elif ptype == "relation":
            ids = [r.get("id") for r in prop.get("relation") or []]
            titles = []
            for rid in ids:
                related = inv.pages.get(rid) if inv else None
                titles.append(page_title(related) if related else None)
            out[name] = {"type": "relation", "relation": [
                {"id": rid, "title": title} for rid, title in zip(ids, titles)
            ]}
        else:
            out[name] = sanitize_node(prop, file_store, sink)
    return out


def build_page_sidecar(page: dict, inv, users_fn, file_store, comments, sink: list) -> dict:
    """The full sidecar of a page.

    :param comments: already-sanitized list, or ``None`` if they were not
        fetched in this run (the caller keeps the previous ones).
    :param sink: list where local attachment paths are accumulated.
    """
    created_by = page.get("created_by") or {}
    edited_by = page.get("last_edited_by") or {}
    sidecar = {
        "id": page["id"],
        "object": "page",
        "title": page_title(page),
        "url": page.get("url"),
        "public_url": page.get("public_url"),
        "created_time": page.get("created_time"),
        "last_edited_time": page.get("last_edited_time"),
        "created_by": {"id": created_by.get("id"), "name": _user_name(users_fn, created_by.get("id"))} if created_by.get("id") else None,
        "last_edited_by": {"id": edited_by.get("id"), "name": _user_name(users_fn, edited_by.get("id"))} if edited_by.get("id") else None,
        "parent": page.get("parent"),
        "parent_id": parent_id_of(page),
        "icon": _sanitize_icon_or_cover(page.get("icon"), file_store, sink),
        "cover": _sanitize_icon_or_cover(page.get("cover"), file_store, sink),
        "in_trash": bool(page.get("in_trash")),
        "is_locked": bool(page.get("is_locked")),
        "properties": _sanitize_properties(
            page.get("properties"), inv, file_store, users_fn, sink
        ),
        "files": sorted(set(sink)),
    }
    if comments is not None:
        sidecar["comments"] = comments
    return sidecar


def sanitize_comments(comments: list, users_fn, file_store, sink: list) -> list:
    """Comments with resolved authors and located attachments."""
    out = []
    for comment in comments or []:
        author = comment.get("created_by") or {}
        record = {
            "id": comment.get("id"),
            "created_time": comment.get("created_time"),
            "last_edited_time": comment.get("last_edited_time"),
            "created_by": {"id": author.get("id"), "name": _user_name(users_fn, author.get("id"))} if author.get("id") else None,
            "text": "".join(
                rt.get("plain_text", "") for rt in comment.get("text") or []
            ),
            "resolved": comment.get("resolved"),
        }
        attachments = comment.get("attachments") or []
        if attachments:
            record["attachments"] = [
                sanitize_file_object(a, file_store, sink) for a in attachments
            ]
        discussion = comment.get("discussion_id")
        if discussion:
            record["discussion_id"] = discussion
        out.append(record)
    return sorted(out, key=lambda c: (c.get("created_time") or "", c.get("id") or ""))


def build_database_sidecar(database: dict | None, data_source: dict, file_store, users_fn, sink: list) -> dict:
    """The sidecar of a database: sanitized database + data source objects."""
    def sanitize_db(db: dict | None) -> dict | None:
        if not db:
            return None
        created_by = db.get("created_by") or {}
        edited_by = db.get("last_edited_by") or {}
        return {
            "id": db.get("id"),
            "title": database_title(db),
            "description": "".join(
                rt.get("plain_text", "") for rt in db.get("description") or []
            ),
            "url": db.get("url"),
            "created_time": db.get("created_time"),
            "last_edited_time": db.get("last_edited_time"),
            "created_by": {"id": created_by.get("id"), "name": _user_name(users_fn, created_by.get("id"))} if created_by.get("id") else None,
            "last_edited_by": {"id": edited_by.get("id"), "name": _user_name(users_fn, edited_by.get("id"))} if edited_by.get("id") else None,
            "parent": db.get("parent"),
            "is_inline": db.get("is_inline"),
            "in_trash": bool(db.get("in_trash")),
            "icon": _sanitize_icon_or_cover(db.get("icon"), file_store, sink),
            "cover": _sanitize_icon_or_cover(db.get("cover"), file_store, sink),
        }

    created_by = data_source.get("created_by") or {}
    edited_by = data_source.get("last_edited_by") or {}
    ds = {
        "id": data_source.get("id"),
        "title": data_source_title(data_source),
        "description": "".join(
            rt.get("plain_text", "") for rt in data_source.get("description") or []
        ),
        "url": data_source.get("url"),
        "created_time": data_source.get("created_time"),
        "last_edited_time": data_source.get("last_edited_time"),
        "created_by": {"id": created_by.get("id"), "name": _user_name(users_fn, created_by.get("id"))} if created_by.get("id") else None,
        "last_edited_by": {"id": edited_by.get("id"), "name": _user_name(users_fn, edited_by.get("id"))} if edited_by.get("id") else None,
        "parent": data_source.get("parent"),
        "database_parent": data_source.get("database_parent"),
        "is_inline": data_source.get("is_inline"),
        "in_trash": bool(data_source.get("in_trash")),
        "properties": sanitize_node(data_source.get("properties"), file_store, sink),
        "icon": _sanitize_icon_or_cover(data_source.get("icon"), file_store, sink),
        "cover": _sanitize_icon_or_cover(data_source.get("cover"), file_store, sink),
    }
    return {"database": sanitize_db(database), "data_source": ds, "files": sorted(set(sink))}
