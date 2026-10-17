"""Picture search keeps spaces apart.

Real: the database, the permission evaluator, the picture vector store (qdrant-client's
in-memory engine). Stood in: the image model (colour and colour-word to a one-hot vector).
"""

from __future__ import annotations

import io
import uuid

import pytest
from PIL import Image
from qdrant_client import AsyncQdrantClient
from sqlalchemy import delete, update

from quberi_api.core.config import get_settings
from quberi_api.domain.documents.models import Document
from quberi_api.domain.identity.models import User
from quberi_api.domain.spaces.models import Space
from quberi_api.services import members as members_service
from quberi_api.services import space_bindings as space_bindings_service
from quberi_api.services.ai.local_image_embedding import SIGLIP2_DIM, prepare_image
from quberi_api.services.rag import image_search
from quberi_api.services.rag.image_store import ImageVectorStore
from tests.test_image_indexing import _doc, _space

MODEL = "siglip2-base"
COLOURS = {"red": 0, "green": 1, "blue": 2}


def _png(colour: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), colour).save(buf, format="PNG")
    return buf.getvalue()


def _onehot(i: int) -> list[float]:
    v = [0.0] * SIGLIP2_DIM
    v[i] = 1.0
    return v


RED, GREEN, BLUE = (_onehot(i) for i in range(3))


class FakeEmbedder:
    """Pictures and sentences meet in one space: the colour decides the direction."""

    async def embed_images(self, images):
        # The real decoder, so bad files are refused exactly as in production.
        out = []
        for data in images:
            r, g, b = prepare_image(data).getpixel((0, 0))
            out.append(_onehot(max(range(3), key=lambda i: (r, g, b)[i])))
        return out

    async def embed_texts(self, texts):
        return [_onehot(next((i for w, i in COLOURS.items() if w in t.lower()), 2)) for t in texts]


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch):
    monkeypatch.setattr(image_search, "get_image_embedder", lambda *_a, **_k: FakeEmbedder())


@pytest.fixture
async def qdrant():
    client = AsyncQdrantClient(":memory:")
    yield client
    await client.close()


async def _member(db, boot, role="member"):
    await db.commit()
    view = await members_service.invite_member(
        db,
        organization_id=boot.organization_id,
        invited_by_user_id=boot.user_id,
        email=f"{role}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=role.title(),
        role_code=role,
    )
    user = await db.get(User, view.user.id)
    user.status = "active"
    await db.flush()
    await db.commit()
    return user


async def _grant(db, boot, space, user, role="viewer"):
    await db.commit()
    await space_bindings_service.add_binding(
        db,
        space_id=space.id,
        organization_id=boot.organization_id,
        actor_user_id=boot.user_id,
        subject_type="user",
        subject_id=user.id,
        role_code=role,
    )
    await db.commit()


async def _revoke(db, boot, space, user):
    await db.commit()
    bindings = await space_bindings_service.list_bindings(
        db, space_id=space.id, organization_id=boot.organization_id
    )
    mine = [b for b in bindings if b.subject_id == user.id]
    assert mine, "the test expected a binding to remove"
    await db.commit()
    for view in mine:
        await space_bindings_service.remove_binding(
            db,
            binding_id=view.binding.id,
            space_id=space.id,
            organization_id=boot.organization_id,
            actor_user_id=boot.user_id,
        )
    await db.commit()


async def _indexed_picture(db, boot, qdrant, space, vec, name="part.png"):
    """A picture document that is indexed, with its vector filed under its space."""

    doc = await _doc(db, boot, space, name, state="indexed")
    await ImageVectorStore(qdrant, model_id=MODEL, vector_dim=SIGLIP2_DIM).upsert_image(
        organization_id=boot.organization_id,
        space_id=space.id,
        document_id=doc.id,
        document_name=name,
        mime_type="image/png",
        document_checksum=None,
        vector=vec,
    )
    return doc


async def _search(db, boot, qdrant, who, vec, requested=None, top_k=12):
    scope = await image_search.resolve_image_scope(
        db,
        organization_id=boot.organization_id,
        caller_user_id=who.id if hasattr(who, "id") else who,
        requested_space_ids=requested,
    )
    result = await image_search.search_images(
        db,
        qdrant=qdrant,
        organization_id=boot.organization_id,
        space_ids=scope,
        query_vector=vec,
        top_k=top_k,
    )
    return scope, result


# ---------------------------------------------------------------------------
# Where to look: the intersection
# ---------------------------------------------------------------------------


async def test_no_preference_means_every_space_the_person_can_read(db_session, org_and_owner):
    team = await _space(db_session, org_and_owner, "Team")
    scope = await image_search.resolve_image_scope(
        db_session,
        organization_id=org_and_owner.organization_id,
        caller_user_id=org_and_owner.user_id,
    )
    assert team.id in scope
    assert org_and_owner.personal_space_id in scope


async def test_asking_for_a_space_you_cannot_read_adds_nothing(db_session, org_and_owner):
    secret = await _space(db_session, org_and_owner, "Secret")
    shared = await _space(db_session, org_and_owner, "Shared")
    member = await _member(db_session, org_and_owner)
    await _grant(db_session, org_and_owner, shared, member)

    scope = await image_search.resolve_image_scope(
        db_session,
        organization_id=org_and_owner.organization_id,
        caller_user_id=member.id,
        requested_space_ids=[secret.id, shared.id],
    )

    assert scope == [shared.id]


async def test_the_choice_only_narrows_it_never_widens_it(db_session, org_and_owner):
    a = await _space(db_session, org_and_owner, "A")
    b = await _space(db_session, org_and_owner, "B")
    member = await _member(db_session, org_and_owner)
    await _grant(db_session, org_and_owner, a, member)
    await _grant(db_session, org_and_owner, b, member)

    only_a = await image_search.resolve_image_scope(
        db_session,
        organization_id=org_and_owner.organization_id,
        caller_user_id=member.id,
        requested_space_ids=[a.id],
    )

    assert only_a == [a.id]


async def test_asking_for_nothing_searches_nothing(db_session, org_and_owner):
    await _space(db_session, org_and_owner, "A")
    scope = await image_search.resolve_image_scope(
        db_session,
        organization_id=org_and_owner.organization_id,
        caller_user_id=org_and_owner.user_id,
        requested_space_ids=[],
    )
    assert scope == []


async def test_a_space_from_another_organisation_is_never_in_scope(db_session, org_and_owner):
    foreign = uuid.uuid4()
    scope = await image_search.resolve_image_scope(
        db_session,
        organization_id=org_and_owner.organization_id,
        caller_user_id=org_and_owner.user_id,
        requested_space_ids=[foreign],
    )
    assert scope == []


async def test_losing_access_takes_effect_on_the_very_next_search(
    db_session, org_and_owner, qdrant
):
    space = await _space(db_session, org_and_owner, "Parts")
    member = await _member(db_session, org_and_owner)
    await _grant(db_session, org_and_owner, space, member)
    await _indexed_picture(db_session, org_and_owner, qdrant, space, RED)

    _, before = await _search(db_session, org_and_owner, qdrant, member, RED)
    await _revoke(db_session, org_and_owner, space, member)
    _, after = await _search(db_session, org_and_owner, qdrant, member, RED)

    assert [m.document_name for m in before.matches] == ["part.png"]
    assert after.matches == []


# ---------------------------------------------------------------------------
# What a search returns
# ---------------------------------------------------------------------------


async def test_finds_the_nearest_picture_with_its_space(db_session, org_and_owner, qdrant):
    space = await _space(db_session, org_and_owner, "Parts")
    red = await _indexed_picture(db_session, org_and_owner, qdrant, space, RED, "red.png")
    await _indexed_picture(db_session, org_and_owner, qdrant, space, BLUE, "blue.png")

    scope, result = await _search(db_session, org_and_owner, qdrant, org_and_owner.user_id, RED)

    top = result.matches[0]
    assert (top.document_id, top.document_name, top.space_id, top.space_name) == (
        red.id, "red.png", space.id, "Parts"
    )
    assert top.mime_type == "image/png"
    assert top.score > result.matches[1].score
    assert space.id in result.space_ids and space.id in scope


async def test_a_picture_in_a_space_you_cannot_read_is_never_returned(
    db_session, org_and_owner, qdrant
):
    mine = await _space(db_session, org_and_owner, "Mine")
    hidden = await _space(db_session, org_and_owner, "Hidden")
    member = await _member(db_session, org_and_owner)
    await _grant(db_session, org_and_owner, mine, member)
    await _indexed_picture(db_session, org_and_owner, qdrant, mine, RED, "mine.png")
    # Same vector as the visible one, so only the scope can keep it out.
    await _indexed_picture(db_session, org_and_owner, qdrant, hidden, RED, "hidden.png")

    _, result = await _search(db_session, org_and_owner, qdrant, member, RED)

    assert [m.document_name for m in result.matches] == ["mine.png"]


async def test_another_organisations_picture_is_never_returned(
    db_session, org_and_owner, qdrant
):
    space = await _space(db_session, org_and_owner, "Mine")
    await _indexed_picture(db_session, org_and_owner, qdrant, space, RED, "mine.png")
    # A vector filed under another organisation, in a space with the same id.
    await ImageVectorStore(qdrant, model_id=MODEL, vector_dim=SIGLIP2_DIM).upsert_image(
        organization_id=uuid.uuid4(), space_id=space.id, document_id=uuid.uuid4(),
        document_name="foreign.png", mime_type="image/png", document_checksum=None, vector=RED,
    )

    _, result = await _search(db_session, org_and_owner, qdrant, org_and_owner.user_id, RED)

    assert [m.document_name for m in result.matches] == ["mine.png"]


async def test_choosing_one_space_searches_only_that_one(db_session, org_and_owner, qdrant):
    a = await _space(db_session, org_and_owner, "A")
    b = await _space(db_session, org_and_owner, "B")
    await _indexed_picture(db_session, org_and_owner, qdrant, a, RED, "in-a.png")
    await _indexed_picture(db_session, org_and_owner, qdrant, b, RED, "in-b.png")

    _, result = await _search(
        db_session, org_and_owner, qdrant, org_and_owner.user_id, RED, requested=[b.id]
    )

    assert [m.document_name for m in result.matches] == ["in-b.png"]
    assert result.space_ids == [b.id]


async def test_an_empty_scope_returns_nothing_without_touching_the_vector_store(
    db_session, org_and_owner, qdrant
):
    calls = []

    class Spy:
        def __getattr__(self, name):
            if name == "query_points":
                async def spy(*a, **k):
                    calls.append(k)
                    return await qdrant.query_points(*a, **k)
                return spy
            return getattr(qdrant, name)

    result = await image_search.search_images(
        db_session, qdrant=Spy(), organization_id=org_and_owner.organization_id,
        space_ids=[], query_vector=RED,
    )

    assert result.matches == [] and result.space_ids == []
    assert calls == []


# ---------------------------------------------------------------------------
# Stale vectors never reach the person
# ---------------------------------------------------------------------------


async def test_a_deleted_documents_leftover_vector_is_dropped(db_session, org_and_owner, qdrant):
    space = await _space(db_session, org_and_owner, "Parts")
    gone = await _indexed_picture(db_session, org_and_owner, qdrant, space, RED, "gone.png")
    await _indexed_picture(db_session, org_and_owner, qdrant, space, RED, "kept.png")
    await db_session.execute(delete(Document).where(Document.id == gone.id))
    await db_session.commit()

    _, result = await _search(db_session, org_and_owner, qdrant, org_and_owner.user_id, RED)

    assert [m.document_name for m in result.matches] == ["kept.png"]


async def test_an_archived_document_is_dropped(db_session, org_and_owner, qdrant):
    space = await _space(db_session, org_and_owner, "Parts")
    old = await _indexed_picture(db_session, org_and_owner, qdrant, space, RED, "old.png")
    await db_session.execute(
        update(Document).where(Document.id == old.id).values(status="archived")
    )
    await db_session.commit()

    _, result = await _search(db_session, org_and_owner, qdrant, org_and_owner.user_id, RED)

    assert result.matches == []


async def test_a_picture_that_is_no_longer_indexed_is_dropped(db_session, org_and_owner, qdrant):
    space = await _space(db_session, org_and_owner, "Parts")
    doc = await _indexed_picture(db_session, org_and_owner, qdrant, space, RED)
    await db_session.execute(
        update(Document).where(Document.id == doc.id).values(indexing_state="failed")
    )
    await db_session.commit()

    _, result = await _search(db_session, org_and_owner, qdrant, org_and_owner.user_id, RED)

    assert result.matches == []


async def test_a_picture_in_an_archived_space_is_dropped(db_session, org_and_owner, qdrant):
    space = await _space(db_session, org_and_owner, "Parts")
    await _indexed_picture(db_session, org_and_owner, qdrant, space, RED)
    await db_session.execute(update(Space).where(Space.id == space.id).values(status="archived"))
    await db_session.commit()

    _, result = await _search(db_session, org_and_owner, qdrant, org_and_owner.user_id, RED)

    assert result.matches == []


async def test_a_vector_that_names_the_wrong_space_is_dropped(db_session, org_and_owner, qdrant):
    """The document moved but its vector still says the old space: do not trust the vector."""

    old_home = await _space(db_session, org_and_owner, "Old")
    new_home = await _space(db_session, org_and_owner, "New")
    doc = await _indexed_picture(db_session, org_and_owner, qdrant, old_home, RED)
    await db_session.execute(
        update(Document).where(Document.id == doc.id).values(space_id=new_home.id)
    )
    await db_session.commit()

    _, result = await _search(db_session, org_and_owner, qdrant, org_and_owner.user_id, RED)

    assert result.matches == []


async def test_stale_hits_do_not_crowd_out_good_ones(db_session, org_and_owner, qdrant):
    space = await _space(db_session, org_and_owner, "Parts")
    store = ImageVectorStore(qdrant, model_id=MODEL, vector_dim=SIGLIP2_DIM)
    for i in range(4):  # closest to the query, but their documents do not exist
        await store.upsert_image(
            organization_id=org_and_owner.organization_id, space_id=space.id,
            document_id=uuid.uuid4(), document_name=f"ghost{i}.png", mime_type="image/png",
            document_checksum=None, vector=RED,
        )
    near = [0.9] + [0.0] * (SIGLIP2_DIM - 1)
    near[1] = 0.4
    for name in ("real1.png", "real2.png"):
        doc = await _doc(db_session, org_and_owner, space, name, state="indexed")
        await store.upsert_image(
            organization_id=org_and_owner.organization_id, space_id=space.id, document_id=doc.id,
            document_name=name, mime_type="image/png", document_checksum=None, vector=near,
        )

    _, result = await _search(
        db_session, org_and_owner, qdrant, org_and_owner.user_id, RED, top_k=2
    )

    assert sorted(m.document_name for m in result.matches) == ["real1.png", "real2.png"]


async def test_top_k_is_capped(db_session, org_and_owner, qdrant):
    space = await _space(db_session, org_and_owner, "Parts")
    for i in range(5):
        await _indexed_picture(db_session, org_and_owner, qdrant, space, RED, f"{i}.png")
    _, result = await _search(
        db_session, org_and_owner, qdrant, org_and_owner.user_id, RED, top_k=3
    )
    assert len(result.matches) == 3
    _, none = await _search(db_session, org_and_owner, qdrant, org_and_owner.user_id, RED, top_k=0)
    assert len(none.matches) == 1  # never below one


async def test_top_k_has_an_upper_limit(db_session, org_and_owner, qdrant, monkeypatch):
    monkeypatch.setattr(image_search, "MAX_TOP_K", 3)
    space = await _space(db_session, org_and_owner, "Parts")
    for i in range(6):
        await _indexed_picture(db_session, org_and_owner, qdrant, space, RED, f"{i}.png")
    _, result = await _search(
        db_session, org_and_owner, qdrant, org_and_owner.user_id, RED, top_k=1000
    )
    assert len(result.matches) == 3


async def test_a_stray_row_from_another_organisation_is_not_returned(
    db_session, org_and_owner, qdrant
):
    """Should never exist, but if a document row of another organisation sat in this space
    and a vector claimed it, the organisation check on the row still stops it."""

    from quberi_api.services import onboarding

    space = await _space(db_session, org_and_owner, "Parts")
    stray = await _indexed_picture(db_session, org_and_owner, qdrant, space, RED, "stray.png")
    await db_session.commit()
    other = await onboarding.bootstrap_organization(
        db_session,
        name="Other",
        slug=f"other-{uuid.uuid4().hex[:8]}",
        owner_email=f"o-{uuid.uuid4().hex[:8]}@example.com",
        owner_display_name="Other Owner",
    )
    await db_session.execute(
        update(Document)
        .where(Document.id == stray.id)
        .values(organization_id=other.organization_id)
    )
    await db_session.commit()

    _, result = await _search(db_session, org_and_owner, qdrant, org_and_owner.user_id, RED)

    assert result.matches == []


# ---------------------------------------------------------------------------
# The query
# ---------------------------------------------------------------------------


async def test_a_picture_a_sentence_or_both_make_one_query_vector():
    by_picture = await image_search.embed_query(image=_png("red"), text=None)
    by_words = await image_search.embed_query(image=None, text="a green part")
    both = await image_search.embed_query(image=_png("red"), text="a green part")

    assert by_picture == RED and by_words == GREEN
    assert both[0] == pytest.approx(0.7071, abs=1e-3) and both[1] == pytest.approx(0.7071, abs=1e-3)
    assert sum(x * x for x in both) == pytest.approx(1.0)


async def test_an_empty_query_is_refused():
    with pytest.raises(image_search.ImageSearchError, match="picture, a description, or both"):
        await image_search.embed_query(image=None, text="   ")


async def test_a_very_long_description_is_refused():
    with pytest.raises(image_search.ImageSearchError, match="longer than"):
        await image_search.embed_query(image=None, text="x" * 501)
    await image_search.embed_query(image=None, text="x" * 500)  # exactly at the limit is fine


async def test_a_switched_off_server_says_so(monkeypatch):
    monkeypatch.setattr(get_settings(), "image_search_enabled", False)
    with pytest.raises(image_search.ImageSearchDisabled):
        await image_search.embed_query(image=_png("red"), text=None)


async def test_the_query_picture_is_never_stored_or_logged(monkeypatch, caplog):
    from quberi_api.services.documents.connectors import local as local_connector

    async def forbidden(*_a, **_k):
        raise AssertionError("the query picture must not be stored")

    monkeypatch.setattr(local_connector, "store", forbidden)
    data = _png("red")
    with caplog.at_level("DEBUG"):
        await image_search.embed_query(image=data, text="a red part")
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "a red part" not in logged
    assert repr(data[:16]) not in logged
