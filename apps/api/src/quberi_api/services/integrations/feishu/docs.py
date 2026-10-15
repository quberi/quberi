"""Feishu cloud-docs surface — list files, fetch content.

Two API families we care about early:

* **Drive** (``/drive/v1/files``) — the tenant's regular cloud files:
  documents, spreadsheets, presentations, folders, media. Listing is
  folder-scoped; the empty ``folder_token`` returns the root of "my
  space" for the authorising user.
* **Docx** (``/docx/v1/documents/{document_id}/raw_content``) — the
  plain-text render of a new-format document. Perfect for indexing;
  spreadsheets and slides need separate endpoints (TODO).

Wiki (``/wiki/v2/spaces``) — the team knowledge base — is a separate
top-level surface; we'll add it when a customer needs it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from quberi_api.services.integrations.feishu.client import FeishuClient, FeishuError

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FeishuFile:
    """One row in a cloud-drive folder listing."""

    token: str  # opaque doc/file id; used to read content
    name: str
    type: str  # "docx" | "doc" | "sheet" | "bitable" | "mindnote" | "file" | "folder" | ...
    parent_token: Optional[str]
    url: str
    owner_id: Optional[str]
    created_time: Optional[str]  # ISO-ish; feishu returns seconds since epoch
    modified_time: Optional[str]


# ---------------------------------------------------------------------------
# APIs
# ---------------------------------------------------------------------------


async def list_files(
    client: FeishuClient,
    *,
    folder_token: Optional[str] = None,
    page_size: int = 50,
) -> list[FeishuFile]:
    """List files in a folder. ``folder_token=None`` = tenant root.

    Feishu paginates with ``page_token``; we fetch up to ~200 rows here
    to keep the smoke-test surface simple. A full sync loop lives in
    the indexer path later.
    """

    files: list[FeishuFile] = []
    page_token: Optional[str] = None
    fetched_pages = 0
    while True:
        fetched_pages += 1
        params: dict = {"page_size": page_size}
        if folder_token:
            params["folder_token"] = folder_token
        if page_token:
            params["page_token"] = page_token
        data = await client.request(
            "GET", "/drive/v1/files", params=params
        )
        for row in (data or {}).get("files", []) or []:
            files.append(
                FeishuFile(
                    token=row.get("token", ""),
                    name=row.get("name", ""),
                    type=row.get("type", ""),
                    parent_token=row.get("parent_token"),
                    url=row.get("url", ""),
                    owner_id=row.get("owner_id"),
                    created_time=row.get("created_time"),
                    modified_time=row.get("modified_time"),
                )
            )
        page_token = (data or {}).get("next_page_token")
        if not page_token or fetched_pages >= 4:
            break
    return files


async def get_document_text(
    client: FeishuClient, *, document_id: str
) -> str:
    """Fetch a docx's plain-text rendering.

    ``document_id`` is the ``token`` from :func:`list_files` where
    ``type == "docx"``. Feishu also returns an ``content`` field with
    the raw text; formatting (headings, tables) is flattened.
    """

    data = await client.request(
        "GET", f"/docx/v1/documents/{document_id}/raw_content"
    )
    return (data or {}).get("content", "") or ""


# ---------------------------------------------------------------------------
# User-token variants — act *as the authorising user* rather than the app.
# These power the folder picker + permission-faithful retrieval, where the
# set of visible files must match what the human can see, not what the app
# was granted at tenant scope.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FolderNode:
    """A node in the browsable folder tree."""

    token: str
    name: str
    type: str  # "folder" | "docx" | "sheet" | ...
    has_children: bool


async def list_files_as_user(
    *,
    base_url: str,
    user_access_token: str,
    folder_token: Optional[str] = None,
    page_size: int = 50,
) -> list[FeishuFile]:
    """List files in a folder using a *user* access token.

    Mirrors :func:`list_files` but authenticates as the human. Returns
    the same ``FeishuFile`` shape.
    """

    files: list[FeishuFile] = []
    page_token: Optional[str] = None
    fetched_pages = 0
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
        while True:
            fetched_pages += 1
            params: dict = {"page_size": page_size}
            if folder_token:
                params["folder_token"] = folder_token
            if page_token:
                params["page_token"] = page_token
            resp = await http.get(
                f"{base_url}/drive/v1/files",
                params=params,
                headers={"Authorization": f"Bearer {user_access_token}"},
            )
            body = resp.json()
            if body.get("code") not in (0, None):
                raise FeishuError(
                    body.get("code", -1),
                    body.get("msg", "list_files (user) failed"),
                    detail=body,
                )
            data = body.get("data") or {}
            for row in data.get("files", []) or []:
                files.append(
                    FeishuFile(
                        token=row.get("token", ""),
                        name=row.get("name", ""),
                        type=row.get("type", ""),
                        parent_token=row.get("parent_token"),
                        url=row.get("url", ""),
                        owner_id=row.get("owner_id"),
                        created_time=row.get("created_time"),
                        modified_time=row.get("modified_time"),
                    )
                )
            page_token = data.get("next_page_token")
            if not page_token or fetched_pages >= 4:
                break
    return files


async def user_can_open_folder(
    *,
    base_url: str,
    user_access_token: str,
    folder_token: str,
) -> None:
    """Prove that the *person* (not the app) can open a Feishu folder.

    Asks Feishu for the first entry of the folder with the user's own token.
    Returns normally when Feishu answers; raises :class:`FeishuError` when it
    refuses (no access, unknown folder, bad token). An empty folder is fine.
    """

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
        resp = await http.get(
            f"{base_url}/drive/v1/files",
            params={"folder_token": folder_token, "page_size": 1},
            headers={"Authorization": f"Bearer {user_access_token}"},
        )
    body = resp.json()
    if body.get("code") not in (0, None):
        raise FeishuError(
            body.get("code", -1),
            body.get("msg", "folder not accessible"),
            detail=body,
        )


async def browse_folder_as_user(
    *,
    base_url: str,
    user_access_token: str,
    folder_token: Optional[str] = None,
) -> list[FolderNode]:
    """One level of the folder tree, folders first, for the UI picker.

    Non-recursive by design — the frontend lazy-expands each folder as
    the user drills in, so we never pull the whole tree up front.
    """

    files = await list_files_as_user(
        base_url=base_url,
        user_access_token=user_access_token,
        folder_token=folder_token,
    )
    nodes = [
        FolderNode(
            token=f.token,
            name=f.name,
            type=f.type,
            has_children=f.type == "folder",
        )
        for f in files
    ]
    # Folders first, then docs; each group alphabetical.
    nodes.sort(key=lambda n: (n.type != "folder", n.name))
    return nodes
