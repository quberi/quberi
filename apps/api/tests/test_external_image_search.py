# ruff: noqa: F811  (fixtures are imported by name and used as pytest arguments)
"""Picture search from an outside tool, and the short-lived preview links it hands out.

Real: the database, the permission evaluator, the picture vector store (qdrant-client's
in-memory engine), the OAuth grant. Stood in: the picture model and the file store.
"""

from __future__ import annotations

import io
import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image
from pydantic import SecretStr
from qdrant_client import AsyncQdrantClient
from sqlalchemy import select, update

from quberi_api.api import dependencies
from quberi_api.api.v1.external import external_router
from quberi_api.core.config import get_settings
from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.documents.models import Document
from quberi_api.domain.identity.models import Organization
from quberi_api.domain.oauth.models import OAuthGrant
from quberi_api.domain.spaces.models import Space
from quberi_api.services import external_image_links as links
from tests.test_external_save import _auth, _org, _tokens, redis_client  # noqa: F401
from tests.test_image_indexing import _doc
from tests.test_image_indexing import _space as _make_space
from tests.test_image_search import (  # noqa: F401
    BLUE,
    GREEN,
    RED,
    _grant,
    _indexed_picture,
    _member,
    _revoke,
    fake_embedder,
)
from tests.test_image_search_api import blobs  # noqa: F401

SEARCH = "/api/v1/external/images/search"
ORIGIN = "https://api.example.test"


def _png(colour: str = "red", size=(1600, 900)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def signing_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "external_link_signing_key", SecretStr("test-signing-key"))
    monkeypatch.setattr(get_settings(), "public_base_url", ORIGIN)


@pytest.fixture
async def qdrant():
    client = AsyncQdrantClient(":memory:")
    yield client
    await client.close()


@pytest.fixture
async def http(db_session, redis_client, qdrant):
    app = FastAPI()
    app.state.settings = get_settings()
    app.include_router(external_router, prefix="/api/v1")
    infra = SimpleNamespace(redis=redis_client, qdrant=qdrant)

    async def _session():
        yield db_session

    app.dependency_overrides[dependencies.get_db_session] = _session
    app.dependency_overrides[dependencies.get_infrastructure] = lambda: infra
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _read(db, redis, boot, user_id=None, scope="knowledge:read"):
    tokens = await _tokens(db, redis, boot, scope=scope, user_id=user_id)
    await db.commit()
    return tokens


async def _stored(db, boot, qdrant, space, vec, blobs, name="part.png", colour="red"):
    """An indexed picture whose bytes can really be read back for a preview."""
    doc = await _doc(db, boot, space, name, state="indexed", blobs=blobs, data=_png(colour))
    from quberi_api.services.ai.local_image_embedding import SIGLIP2_DIM
    from quberi_api.services.rag.image_store import ImageVectorStore

    await ImageVectorStore(qdrant, model_id="siglip2-base", vector_dim=SIGLIP2_DIM).upsert_image(
        organization_id=boot.organization_id,
        space_id=space.id,
        document_id=doc.id,
        document_name=name,
        mime_type="image/png",
        document_checksum=None,
        vector=vec,
    )
    return doc


def _path(url: str) -> str:
    assert url.startswith(f"{ORIGIN}/api/v1/external/preview/") and url.endswith(".jpg"), url
    return url.removeprefix(ORIGIN)


# ---------------------------------------------------------------------------
# The link itself
# ---------------------------------------------------------------------------


def _ids():
    return uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def test_a_link_round_trips_and_is_short():
    settings = get_settings()
    user, grant, doc = _ids()
    token = links.sign(settings, user_id=user, grant_id=grant, document_id=doc)
    found = links.verify(settings, token)
    assert (found.user_id, found.grant_id, found.document_id) == (user, grant, doc)
    # A model has to copy this verbatim, so it must stay short.
    assert len(token) <= 100


def test_changing_any_character_breaks_the_link():
    settings = get_settings()
    token = links.sign(settings, user_id=uuid.uuid4(), grant_id=uuid.uuid4(), document_id=uuid.uuid4())
    import base64

    def decoded(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    for i in range(len(token)):
        flipped = "A" if token[i] != "A" else "B"
        changed = token[:i] + flipped + token[i + 1 :]
        if decoded(changed) == decoded(token):
            continue  # the last character can carry unused bits: nothing actually changed
        assert links.verify(settings, changed) is None, i
    assert links.verify(settings, token[:-1]) is None
    assert links.verify(settings, token + "A") is None
    assert links.verify(settings, "") is None
    assert links.verify(settings, "x" * 500) is None


def test_a_link_expires_and_a_different_key_does_not_verify(monkeypatch):
    settings = get_settings()
    user, grant, doc = _ids()
    now = time.time()
    token = links.sign(settings, user_id=user, grant_id=grant, document_id=doc, now=now - 700)
    assert links.verify(settings, token) is None  # signed 700 s ago, valid for 600
    fresh = links.sign(settings, user_id=user, grant_id=grant, document_id=doc)
    assert links.verify(settings, fresh) is not None
    monkeypatch.setattr(settings, "external_link_signing_key", SecretStr("another-key"))
    assert links.verify(settings, fresh) is None


def test_without_a_key_no_links_are_made(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "external_link_signing_key", None)
    monkeypatch.setattr(settings, "encryption_key", None)
    assert links.links_available(settings) is False
    assert links.verify(settings, "anything") is None
    with pytest.raises(RuntimeError):
        links.sign(settings, user_id=uuid.uuid4(), grant_id=uuid.uuid4(), document_id=uuid.uuid4())


# ---------------------------------------------------------------------------
# Searching
# ---------------------------------------------------------------------------


async def test_a_description_finds_the_closest_pictures_with_preview_links(
    http, db_session, redis_client, qdrant, blobs
):
    boot = await _org(db_session)
    space = await _make_space(db_session, boot, "Parts")
    await _stored(db_session, boot, qdrant, space, RED, blobs, "red-gear.png")
    await _stored(db_session, boot, qdrant, space, BLUE, blobs, "blue-gear.png")
    tokens = await _read(db_session, redis_client, boot)

    resp = await http.post(SEARCH, headers=_auth(tokens), json={"query": "a red gear", "top_k": 2})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [i["name"] for i in body["images"]][0] == "red-gear.png"
    first = body["images"][0]
    assert first["space_name"] == "Parts" and first["mime_type"] == "image/png"
    assert body["preview_expires_in"] == 600
    assert body["searched_spaces"] >= 1
    # Metadata, a link and the document's id (to ask for it larger with get_document_image):
    # never picture bytes.
    assert set(first) == {
        "document_id", "name", "space_name", "mime_type", "size_bytes", "score", "preview_url",
    }
    assert _path(first["preview_url"])


async def test_search_needs_the_read_permission_and_a_token(http, db_session, redis_client):
    boot = await _org(db_session)
    write_only = await _read(db_session, redis_client, boot, scope="knowledge:write")
    resp = await http.post(SEARCH, headers=_auth(write_only), json={"query": "x"})
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "insufficient_scope"
    assert (await http.post(SEARCH, json={"query": "x"})).status_code == 401


async def test_only_spaces_the_person_can_read_are_searched(
    http, db_session, redis_client, qdrant, blobs
):
    boot = await _org(db_session)
    mine = await _make_space(db_session, boot, "Mine")
    secret = await _make_space(db_session, boot, "Secret")
    member = await _member(db_session, boot)
    await _grant(db_session, boot, mine, member)
    await _stored(db_session, boot, qdrant, mine, RED, blobs, "visible.png")
    await _stored(db_session, boot, qdrant, secret, RED, blobs, "hidden.png")
    other = await _org(db_session)
    foreign_space = await _make_space(db_session, other, "Foreign")
    await _stored(db_session, other, qdrant, foreign_space, RED, blobs, "foreign.png")
    tokens = await _read(db_session, redis_client, boot, user_id=member.id)

    body = (await http.post(SEARCH, headers=_auth(tokens), json={"query": "red"})).json()

    names = {i["name"] for i in body["images"]}
    assert "visible.png" in names
    assert not names & {"hidden.png", "foreign.png"}
    assert "hidden.png" not in str(body) and "Secret" not in str(body)


async def test_one_space_can_be_named_and_an_unknown_one_is_a_404(
    http, db_session, redis_client, qdrant, blobs
):
    boot = await _org(db_session)
    a = await _make_space(db_session, boot, "Alpha")
    b = await _make_space(db_session, boot, "Beta")
    await _stored(db_session, boot, qdrant, a, RED, blobs, "in-alpha.png")
    await _stored(db_session, boot, qdrant, b, RED, blobs, "in-beta.png")
    tokens = await _read(db_session, redis_client, boot)

    only_b = (
        await http.post(SEARCH, headers=_auth(tokens), json={"query": "red", "space": "Beta"})
    ).json()
    assert [i["name"] for i in only_b["images"]] == ["in-beta.png"]
    assert only_b["searched_spaces"] == 1

    missing = await http.post(SEARCH, headers=_auth(tokens), json={"query": "red", "space": "Nope"})
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "space_not_found"


async def test_bad_requests_and_a_switched_off_server_are_refused_plainly(
    http, db_session, redis_client, qdrant, monkeypatch
):
    boot = await _org(db_session)
    tokens = await _read(db_session, redis_client, boot)
    headers = _auth(tokens)
    assert (await http.post(SEARCH, headers=headers, json={"query": ""})).status_code == 422
    assert (await http.post(SEARCH, headers=headers, json={"query": "x" * 501})).status_code == 422
    assert (await http.post(SEARCH, headers=headers, json={"query": "x", "top_k": 11})).status_code == 422
    assert (await http.post(SEARCH, headers=headers, json={"query": "x", "extra": 1})).status_code == 422

    monkeypatch.setattr(get_settings(), "image_search_enabled", False)
    off = await http.post(SEARCH, headers=headers, json={"query": "red"})
    assert off.status_code == 503
    assert off.json()["detail"]["code"] == "image_search_disabled"


async def test_searches_are_rate_limited_per_grant(http, db_session, redis_client, qdrant, monkeypatch):
    from quberi_api.services.mcp import adapters

    monkeypatch.setattr(adapters, "IMAGE_SEARCH_RATE_LIMIT", 2)
    boot = await _org(db_session)
    tokens = await _read(db_session, redis_client, boot)
    codes = [
        (await http.post(SEARCH, headers=_auth(tokens), json={"query": "red"})).status_code
        for _ in range(3)
    ]
    assert codes == [200, 200, 429]


async def test_the_audit_names_documents_but_not_the_description(
    http, db_session, redis_client, qdrant, blobs
):
    boot = await _org(db_session)
    space = await _make_space(db_session, boot, "Parts")
    doc = await _stored(db_session, boot, qdrant, space, RED, blobs)
    tokens = await _read(db_session, redis_client, boot)

    await http.post(SEARCH, headers=_auth(tokens), json={"query": "confidential red bracket"})

    event = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.organization_id == boot.organization_id,
                AuditEvent.action == "knowledge.external_image_search",
            )
        )
    ).scalar_one()
    assert event.payload["retrieved_document_ids"] == [str(doc.id)]
    assert event.payload["result_count"] == 1
    assert "confidential" not in str(event.payload)


async def test_the_link_points_at_the_request_origin_when_no_public_url_is_set(
    http, db_session, redis_client, qdrant, blobs, monkeypatch
):
    monkeypatch.setattr(get_settings(), "public_base_url", None)
    boot = await _org(db_session)
    space = await _make_space(db_session, boot, "Parts")
    await _stored(db_session, boot, qdrant, space, RED, blobs)
    tokens = await _read(db_session, redis_client, boot)
    body = (await http.post(SEARCH, headers=_auth(tokens), json={"query": "red"})).json()
    assert body["images"][0]["preview_url"].startswith("http://test/api/v1/external/preview/")


async def test_without_a_signing_key_pictures_are_listed_without_links(
    http, db_session, redis_client, qdrant, blobs, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "external_link_signing_key", None)
    monkeypatch.setattr(settings, "encryption_key", None)
    boot = await _org(db_session)
    space = await _make_space(db_session, boot, "Parts")
    await _stored(db_session, boot, qdrant, space, RED, blobs)
    tokens = await _read(db_session, redis_client, boot)

    body = (await http.post(SEARCH, headers=_auth(tokens), json={"query": "red"})).json()

    assert [i["preview_url"] for i in body["images"]] == [None]


# ---------------------------------------------------------------------------
# Using a link
# ---------------------------------------------------------------------------


async def _found(http, db_session, redis_client, qdrant, blobs, user_id=None, boot=None, space=None):
    boot = boot or await _org(db_session)
    space = space or await _make_space(db_session, boot, "Parts")
    doc = await _stored(db_session, boot, qdrant, space, RED, blobs)
    tokens = await _read(db_session, redis_client, boot, user_id=user_id)
    body = (await http.post(SEARCH, headers=_auth(tokens), json={"query": "red"})).json()
    return boot, space, doc, tokens, _path(body["images"][0]["preview_url"])


async def test_a_link_shows_a_small_plain_jpeg_without_any_header(
    http, db_session, redis_client, qdrant, blobs
):
    *_, path = await _found(http, db_session, redis_client, qdrant, blobs)

    resp = await http.get(path)  # no Authorization header: the link is the credential

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["cache-control"].startswith("private")
    assert resp.headers["referrer-policy"] == "no-referrer"
    image = Image.open(io.BytesIO(resp.content))
    assert image.format == "JPEG" and max(image.size) <= 320


async def test_a_forged_or_expired_link_is_a_plain_404(
    http, db_session, redis_client, qdrant, blobs
):
    boot, space, doc, tokens, path = await _found(http, db_session, redis_client, qdrant, blobs)
    grant = (await db_session.execute(select(OAuthGrant).where(OAuthGrant.user_id == boot.user_id))).scalar_one()

    # Flip a character inside the MAC. The last character of the token only carries two
    # unused padding bits, so changing it can leave the decoded bytes (and the link) intact.
    tampered = path[:-15] + ("A" if path[-15] != "A" else "B") + path[-14:]
    old = links.sign(
        get_settings(), user_id=boot.user_id, grant_id=grant.id, document_id=doc.id, now=time.time() - 3600
    )
    for bad in (tampered, f"/api/v1/external/preview/{old}.jpg", "/api/v1/external/preview/nonsense.jpg"):
        resp = await http.get(bad)
        assert resp.status_code == 404, bad
        assert resp.json() == {"detail": "not found"}


async def test_a_link_for_someone_elses_grant_or_picture_does_not_work(
    http, db_session, redis_client, qdrant, blobs
):
    boot, space, doc, tokens, _ = await _found(http, db_session, redis_client, qdrant, blobs)
    grant = (await db_session.execute(select(OAuthGrant).where(OAuthGrant.user_id == boot.user_id))).scalar_one()
    member = await _member(db_session, boot)
    settings = get_settings()

    # A link that pairs the owner's grant with another person's id.
    mismatched = links.sign(settings, user_id=member.id, grant_id=grant.id, document_id=doc.id)
    # A link for a picture that does not exist.
    nowhere = links.sign(settings, user_id=boot.user_id, grant_id=grant.id, document_id=uuid.uuid4())
    for token in (mismatched, nowhere):
        assert (await http.get(f"/api/v1/external/preview/{token}.jpg")).status_code == 404


async def test_a_link_stops_working_when_the_tool_is_revoked(
    http, db_session, redis_client, qdrant, blobs
):
    boot, space, doc, tokens, path = await _found(http, db_session, redis_client, qdrant, blobs)
    assert (await http.get(path)).status_code == 200

    await db_session.execute(update(OAuthGrant).where(OAuthGrant.user_id == boot.user_id).values(revoked_at=datetime.now(timezone.utc)))
    await db_session.commit()

    assert (await http.get(path)).status_code == 404


async def test_a_link_stops_working_when_the_organization_switches_outside_tools_off(
    http, db_session, redis_client, qdrant, blobs
):
    boot, space, doc, tokens, path = await _found(http, db_session, redis_client, qdrant, blobs)
    org = await db_session.get(Organization, boot.organization_id)
    org.allow_external_tools = False
    await db_session.commit()
    assert (await http.get(path)).status_code == 404


async def test_a_link_stops_working_when_the_person_loses_access_to_the_space(
    http, db_session, redis_client, qdrant, blobs
):
    boot = await _org(db_session)
    space = await _make_space(db_session, boot, "Shared")
    member = await _member(db_session, boot)
    await _grant(db_session, boot, space, member)
    *_, path = await _found(
        http, db_session, redis_client, qdrant, blobs, user_id=member.id, boot=boot, space=space
    )
    assert (await http.get(path)).status_code == 200

    await _revoke(db_session, boot, space, member)

    assert (await http.get(path)).status_code == 404


async def test_a_link_stops_working_when_the_picture_is_archived_deleted_or_not_a_picture(
    http, db_session, redis_client, qdrant, blobs
):
    boot, space, doc, tokens, path = await _found(http, db_session, redis_client, qdrant, blobs)

    await db_session.execute(update(Document).where(Document.id == doc.id).values(status="archived"))
    await db_session.commit()
    assert (await http.get(path)).status_code == 404

    await db_session.execute(update(Document).where(Document.id == doc.id).values(status="uploaded"))
    await db_session.commit()
    assert (await http.get(path)).status_code == 200

    await db_session.execute(update(Document).where(Document.id == doc.id).values(mime_type="application/pdf"))
    await db_session.commit()
    assert (await http.get(path)).status_code == 404

    await db_session.execute(update(Document).where(Document.id == doc.id).values(mime_type="image/png"))
    await db_session.execute(update(Space).where(Space.id == space.id).values(status="archived"))
    await db_session.commit()
    assert (await http.get(path)).status_code == 404


async def test_previews_are_rate_limited_per_grant(
    http, db_session, redis_client, qdrant, blobs, monkeypatch
):
    from quberi_api.api.v1 import external as external_api

    monkeypatch.setattr(external_api, "_PREVIEW_RATE_LIMIT", 2)  # preview_by_link stays in external.py (not an adapter)
    *_, path = await _found(http, db_session, redis_client, qdrant, blobs)
    codes = [(await http.get(path)).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


async def test_a_link_stops_working_when_the_grant_expires_or_loses_its_read_permission(
    http, db_session, redis_client, qdrant, blobs
):
    boot, space, doc, tokens, path = await _found(http, db_session, redis_client, qdrant, blobs)
    where = OAuthGrant.user_id == boot.user_id

    await db_session.execute(
        update(OAuthGrant).where(where).values(scope="knowledge:write")
    )
    await db_session.commit()
    assert (await http.get(path)).status_code == 404

    await db_session.execute(update(OAuthGrant).where(where).values(scope="knowledge:read"))
    await db_session.commit()
    assert (await http.get(path)).status_code == 200

    await db_session.execute(
        update(OAuthGrant).where(where).values(absolute_expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    )
    await db_session.commit()
    assert (await http.get(path)).status_code == 404


async def test_a_link_cannot_name_a_picture_in_another_organization(
    http, db_session, redis_client, qdrant, blobs
):
    boot, space, doc, tokens, _ = await _found(http, db_session, redis_client, qdrant, blobs)
    grant = (await db_session.execute(select(OAuthGrant).where(OAuthGrant.user_id == boot.user_id))).scalar_one()
    other = await _org(db_session)
    foreign_space = await _make_space(db_session, other, "Foreign")
    foreign = await _stored(db_session, other, qdrant, foreign_space, RED, blobs, "foreign.png")

    token = links.sign(get_settings(), user_id=boot.user_id, grant_id=grant.id, document_id=foreign.id)

    assert (await http.get(f"/api/v1/external/preview/{token}.jpg")).status_code == 404
