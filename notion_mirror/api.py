"""Typed wrapper over the Notion API endpoints used by the backup.

API version: 2026-03-11 (databases + data sources model).
"""

from __future__ import annotations

import logging

from .http import NotionAPIError, NotionHTTP

log = logging.getLogger("notion-mirror")


class NotionAPI:
    def __init__(self, http: NotionHTTP):
        self.http = http
        self._users: dict[str, dict | None] = {}

    # ---------------------------------------------------------------- search

    def search_objects(self, value: str):
        """Iterates accessible pages or data sources (value: "page" | "data_source").

        Notion's search index is not a complete inventory; the definitive
        inventory is built by ``inventory.build_inventory`` combining this
        with the tree walk.
        """
        body = {
            "filter": {"property": "object", "value": value},
            "sort": {"timestamp": "last_edited_time", "direction": "ascending"},
        }
        yield from self.http.paginate_post("/v1/search", body, idempotent=True)

    # ----------------------------------------------------------------- pages

    def get_page(self, page_id: str) -> dict | None:
        try:
            return self.http.get_json(f"/v1/pages/{page_id}")
        except NotionAPIError as exc:
            if exc.status == 404 or _is_restricted_resource(exc):
                return None
            raise

    def get_page_markdown(self, page_id: str) -> dict:
        """Markdown (Notion-flavored v2) of the given page or block.

        For very large pages (~20k+ records) the response can be truncated,
        with ``unknown_block_ids`` to re-request sub-trees.
        """
        return self.http.get_json(
            f"/v1/pages/{page_id}/markdown", params={"markdown_version": "v2"}
        )

    def get_page_comments(self, page_id: str) -> list[dict]:
        return list(
            self.http.paginate_get(
                "/v1/comments", params={"block_id": page_id, "page_size": 100}
            )
        )

    # ---------------------------------------------------------------- blocks

    def get_block_children(self, block_id: str):
        yield from self.http.paginate_get(
            f"/v1/blocks/{block_id}/children", params={"page_size": 100}
        )

    def get_block(self, block_id: str) -> dict | None:
        try:
            return self.http.get_json(f"/v1/blocks/{block_id}")
        except NotionAPIError as exc:
            if exc.status == 404 or _is_restricted_resource(exc):
                return None
            raise

    # ---------------------------------------------------- databases (v2 API)

    def get_database(self, database_id: str) -> dict | None:
        try:
            return self.http.get_json(f"/v1/databases/{database_id}")
        except NotionAPIError as exc:
            if exc.status == 404 or _is_restricted_resource(exc):
                return None
            raise

    def get_data_source(self, data_source_id: str) -> dict | None:
        try:
            return self.http.get_json(f"/v1/data_sources/{data_source_id}")
        except NotionAPIError as exc:
            if exc.status == 404 or _is_restricted_resource(exc):
                return None
            raise

    def query_data_source(self, data_source_id: str):
        yield from self.http.paginate_post(
            f"/v1/data_sources/{data_source_id}/query", {}, idempotent=True
        )

    # ------------------------------------------------------------------ users

    def get_user(self, user_id: str) -> dict | None:
        if user_id in self._users:
            return self._users[user_id]
        try:
            user = self.http.get_json(f"/v1/users/{user_id}")
        except NotionAPIError as exc:
            if exc.status == 404 or _is_restricted_resource(exc):
                user = None
            else:
                raise
        self._users[user_id] = user
        return user

    def user_display(self, user: dict | None) -> str:
        """A readable name for a (partial) user object."""
        if not user:
            return ""
        return user.get("name") or user.get("id") or ""

    # ---------------------------------------------------------------- writing

    def create_page(self, parent_page_id: str, title: str, icon: dict | None = None) -> dict:
        body = {
            "parent": {"page_id": parent_page_id},
            "properties": {"title": {"title": [{"text": {"content": title}}]}},
        }
        if icon:
            body["icon"] = icon
        return self.http.post_json("/v1/pages", body)

    def replace_page_markdown(self, page_id: str, markdown: str) -> dict:
        """Replaces the whole content of the page with the given Markdown."""
        return self.http.patch_json(
            f"/v1/pages/{page_id}/markdown",
            {
                "type": "replace_content",
                "replace_content": {"new_str": markdown},
                "markdown_version": "v2",
            },
        )

    def append_blocks(self, parent_id: str, children: list[dict]) -> dict:
        return self.http.patch_json(f"/v1/blocks/{parent_id}/children", {"children": children})

    def create_file_upload(self, filename: str, content_type: str | None) -> dict:
        body = {"mode": "single_part", "filename": filename}
        if content_type:
            body["content_type"] = content_type
        return self.http.post_json("/v1/file_uploads", body)

    def send_file(self, file_upload_id: str, path, filename: str, content_type: str) -> dict:
        return self.http.upload_multipart(
            f"/v1/file_uploads/{file_upload_id}/send", path, filename, content_type
        )


def _is_restricted_resource(exc: NotionAPIError) -> bool:
    """Returns True if the error is a 403 ``restricted_resource`` — the object
    is managed by Notion and isn't accessible via the public API. These are
    treated like 404s: the object is skipped without aborting the backup."""
    return exc.status == 403 and exc.code == "restricted_resource"
