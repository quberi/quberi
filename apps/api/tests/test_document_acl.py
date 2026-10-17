"""Document-level access: share one document (allow), hide one document (deny), deny wins.

Real database, real permission evaluator, real vector filters (qdrant-client's in-memory
engine evaluates the same conditions as the server). Only the embedding model is a stand-in.

What these pin, per the spec: the same answer from every door (search, pictures, lists,
counts, details, downloads, previews); filtering happens in the index, not afterwards;
changes apply to the very next request; two people with different rights never see each
other's results; and a manager who is denied can still manage.
"""

from __future__ import annotations

import io
import uuid
from types import SimpleNamespace

import pytest
from qdrant_client import AsyncQdrantClient
from sqlalchemy import select

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.documents.models import Document, DocumentAccessRule
from quberi_api.services import doc_acl
from quberi_api.services import documents as documents_service
from quberi_api.services import groups as groups_service
from quberi_api.services import spaces as spaces_service
from quberi_api.services.documents import access_rules as rules
from quberi_api.services.documents.errors import DocumentPermissionError
from quberi_api.services.errors import AlreadyExistsError, InvalidInputError, NotFoundError
from quberi_api.services.rag import image_search, retrieve
from quberi_api.services.rag.image_store import ImageVectorStore
from quberi_api.services.rag.vector_store import ChunkPoint, VectorStore
from tests.test_image_indexing import _doc, _space
from tests.test_image_search import RED, FakeEmbedder, _grant, _revoke
from tests.test_image_search import _member as _invite

TEXT_MODEL = "test-embed"
TEXT_DIM = 8
IMAGE_MODEL = "siglip2-base"


# ---------------------------------------------------------------------------
# Stand-ins and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
async def qdrant():
    client = AsyncQdrantClient(":memory:")
    yield client
    await client.close()


@pytest.fixture(autouse=True)
def fake_models(monkeypatch):
    monkeypatch.setattr(image_search, "get_image_embedder", lambda *_a, **_k: FakeEmbedder())

    class _Embedder:
        vector_dim = TEXT_DIM

        async def embed(self, texts, **_kw):
            return [[1.0] + [0.0] * (TEXT_DIM - 1) for _ in texts]

    async def _resolve(_session, *, organization_id):
        return SimpleNamespace(
            provider=_Embedder(), model=SimpleNamespace(model_id=TEXT_MODEL, vector_dim=TEXT_DIM)
        )

    monkeypatch.setattr(retrieve, "resolve_embedding_provider", _resolve)


async def _member(db, boot, role="member"):
    """A new colleague, as plain values (so a rolled-back test step cannot expire them)."""
    user = await _invite(db, boot, role)
    return SimpleNamespace(id=user.id, display_name=user.display_name)


def _text_store(qdrant) -> VectorStore:
    return VectorStore(qdrant, model_id=TEXT_MODEL, vector_dim=TEXT_DIM)


async def _text_doc(db, boot, qdrant, space, name):
    """A document with one searchable passage, filed under its space."""
    doc = await _doc(db, boot, space, name, mime="application/pdf", state="indexed")
    await _text_store(qdrant).upsert_document(
        organization_id=boot.organization_id,
        space_id=space.id,
        document_id=doc.id,
        document_name=name,
        document_checksum=None,
        chunks=[
            ChunkPoint(
                chunk_index=0,
                text=f"passage of {name}",
                vector=[1.0] + [0.0] * (TEXT_DIM - 1),
                char_start=0,
                char_end=10,
            )
        ],
    )
    return SimpleNamespace(id=doc.id, name=name)


async def _picture(db, boot, qdrant, space, name):
    doc = await _doc(db, boot, space, name, state="indexed")
    await ImageVectorStore(qdrant, model_id=IMAGE_MODEL, vector_dim=768).upsert_image(
        organization_id=boot.organization_id,
        space_id=space.id,
        document_id=doc.id,
        document_name=name,
        mime_type="image/png",
        document_checksum=None,
        vector=RED,
    )
    return SimpleNamespace(id=doc.id, name=name)


async def _rule(db, boot, space, doc, effect, subject_type, subject_id, actor=None):
    await db.commit()
    view = await rules.add_rule(
        db,
        organization_id=boot.organization_id,
        actor_user_id=actor or boot.user_id,
        space_id=space.id,
        document_id=doc.id,
        effect=effect,
        subject_type=subject_type,
        subject_id=subject_id,
    )
    await db.commit()
    return view.rule


async def _unrule(db, boot, space, doc, rule, actor=None):
    await db.commit()
    await rules.remove_rule(
        db,
        organization_id=boot.organization_id,
        actor_user_id=actor or boot.user_id,
        space_id=space.id,
        document_id=doc.id,
        rule_id=rule.id,
    )
    await db.commit()


async def _search_names(db, boot, qdrant, who, **kw):
    await db.commit()
    result = await retrieve.retrieve_for_user(
        db,
        qdrant=qdrant,
        organization_id=boot.organization_id,
        caller_user_id=who.id,
        query="anything",
        top_k=10,
        **kw,
    )
    await db.commit()
    return sorted({c.document_name for c in result.chunks})


async def _picture_names(db, boot, qdrant, who, requested=None):
    await db.commit()
    scope = await image_search.resolve_image_access(
        db,
        organization_id=boot.organization_id,
        caller_user_id=who.id,
        requested_space_ids=requested,
    )
    found = await image_search.search_images(
        db,
        qdrant=qdrant,
        organization_id=boot.organization_id,
        space_ids=scope.space_ids,
        query_vector=RED,
        top_k=20,
        doc_access=scope.doc_access,
    )
    await db.commit()
    return sorted(m.document_name for m in found.matches)


async def _listed(db, boot, space, who, **kw):
    await db.commit()
    page = await documents_service.list_documents_in_space(
        db,
        organization_id=boot.organization_id,
        space_id=space.id,
        caller_user_id=who.id,
        **kw,
    )
    await db.commit()
    return sorted(v.document.name for v in page.items), page.total


async def _can(db, boot, who, doc):
    await db.commit()
    ok = await doc_acl.can_read_document(
        db, user_id=who.id, organization_id=boot.organization_id, document_id=doc.id
    )
    await db.commit()
    return ok


async def _opens(db, boot, space, who, doc) -> bool:
    """Whether the detail, text and download doors all open (they must agree)."""
    results = []
    for door in (
        documents_service.get_document,
        documents_service.get_document_text,
        documents_service.open_download_stream,
    ):
        await db.commit()
        try:
            await door(
                db,
                document_id=doc.id,
                organization_id=boot.organization_id,
                space_id=space.id,
                caller_user_id=who.id,
            )
            results.append(True)
        except NotFoundError:
            results.append(False)
        except Exception:  # noqa: BLE001 - the blob/extractor is not under test here
            results.append(True)  # got past the permission gate
        await db.commit()
    assert len(set(results)) == 1, f"doors disagree: {results}"
    return results[0]


@pytest.fixture
async def world(db_session, org_and_owner, qdrant):
    """One team space with a text document and a picture, and an ordinary member."""
    boot = org_and_owner
    space = SimpleNamespace(id=(await _space(db_session, boot, "Secret")).id)
    member = await _member(db_session, boot)
    a = await _text_doc(db_session, boot, qdrant, space, "a.pdf")
    b = await _text_doc(db_session, boot, qdrant, space, "b.pdf")
    pic = await _picture(db_session, boot, qdrant, space, "site.png")
    owner = SimpleNamespace(id=boot.user_id)
    return SimpleNamespace(
        boot=boot, space=space, member=member, owner=owner, a=a, b=b, pic=pic, qdrant=qdrant
    )


# ---------------------------------------------------------------------------
# Tightening: deny hides one document from someone who can read the space
# ---------------------------------------------------------------------------


async def test_a_denied_document_disappears_from_every_door_for_that_person_only(
    db_session, world
):
    w, db = world, db_session
    await _grant(db, w.boot, w.space, w.member)
    assert await _search_names(db, w.boot, w.qdrant, w.member) == ["a.pdf", "b.pdf"]
    assert await _picture_names(db, w.boot, w.qdrant, w.member) == ["site.png"]

    await _rule(db, w.boot, w.space, w.a, "deny", "user", w.member.id)
    await _rule(db, w.boot, w.space, w.pic, "deny", "user", w.member.id)

    # Search, picture search, list, count: all agree.
    assert await _search_names(db, w.boot, w.qdrant, w.member) == ["b.pdf"]
    assert await _picture_names(db, w.boot, w.qdrant, w.member) == []
    assert await _listed(db, w.boot, w.space, w.member) == (["b.pdf"], 1)
    stats = await documents_service.stats_for_space(
        db, organization_id=w.boot.organization_id, space_id=w.space.id, caller_user_id=w.member.id
    )
    assert stats.total == 1
    # Detail, text and download all refuse, and agree.
    assert await _opens(db, w.boot, w.space, w.member, w.a) is False
    assert await _opens(db, w.boot, w.space, w.member, w.pic) is False
    assert await _opens(db, w.boot, w.space, w.member, w.b) is True
    assert await _can(db, w.boot, w.member, w.a) is False
    assert await _can(db, w.boot, w.member, w.b) is True

    # Everyone else is unaffected, including the owner.
    assert await _search_names(db, w.boot, w.qdrant, w.owner) == ["a.pdf", "b.pdf"]
    assert await _listed(db, w.boot, w.space, w.owner) == (["a.pdf", "b.pdf", "site.png"], 3)
    assert await _can(db, w.boot, w.owner, w.a) is True


async def test_a_denial_is_applied_inside_the_index_so_recall_does_not_shrink(
    db_session, world
):
    """With many hidden documents the person still gets a full page of what they may read."""
    w, db = world, db_session
    await _grant(db, w.boot, w.space, w.member)
    hidden = []
    for i in range(6):
        d = await _text_doc(db, w.boot, w.qdrant, w.space, f"hidden{i}.pdf")
        await _rule(db, w.boot, w.space, d, "deny", "user", w.member.id)
        hidden.append(d.id)
    await db.commit()
    result = await retrieve.retrieve_for_user(
        db, qdrant=w.qdrant, organization_id=w.boot.organization_id,
        caller_user_id=w.member.id, query="q", top_k=2,
    )
    # Filtering after the search would leave fewer than top_k whenever the hidden ones rank first.
    assert len(result.chunks) == 2
    assert not {c.document_id for c in result.chunks} & set(hidden)


async def test_removing_a_denial_applies_to_the_very_next_request(db_session, world):
    w, db = world, db_session
    await _grant(db, w.boot, w.space, w.member)
    rule = await _rule(db, w.boot, w.space, w.a, "deny", "user", w.member.id)
    assert await _search_names(db, w.boot, w.qdrant, w.member) == ["b.pdf"]
    await _unrule(db, w.boot, w.space, w.a, rule)
    assert await _search_names(db, w.boot, w.qdrant, w.member) == ["a.pdf", "b.pdf"]
    assert await _can(db, w.boot, w.member, w.a) is True


async def test_a_denial_beats_every_other_way_of_getting_in(db_session, world):
    """Deny wins over a space role, an allow to the same person, and an allow to everyone."""
    w, db = world, db_session
    await _grant(db, w.boot, w.space, w.member, role="editor")
    await _rule(db, w.boot, w.space, w.a, "allow", "user", w.member.id)
    await _rule(db, w.boot, w.space, w.a, "allow", "organization", w.boot.organization_id)
    await _rule(db, w.boot, w.space, w.a, "deny", "user", w.member.id)
    assert await _can(db, w.boot, w.member, w.a) is False
    assert await _search_names(db, w.boot, w.qdrant, w.member) == ["b.pdf"]
    assert await _listed(db, w.boot, w.space, w.member) == (["b.pdf", "site.png"], 2)


async def test_a_denial_through_a_group_beats_an_allow_to_the_person(db_session, world):
    w, db = world, db_session
    await db.commit()
    group = await groups_service.create_group(
        db, organization_id=w.boot.organization_id, actor_user_id=w.boot.user_id,
        name="Contractors",
    )
    await db.commit()
    await groups_service.add_member(
        db, group_id=group.group.id, organization_id=w.boot.organization_id,
        actor_user_id=w.boot.user_id, member_type="user", member_id=w.member.id,
    )
    await db.commit()
    await _grant(db, w.boot, w.space, w.member)
    await _rule(db, w.boot, w.space, w.a, "allow", "user", w.member.id)
    await _rule(db, w.boot, w.space, w.a, "deny", "group", group.group.id)
    assert await _can(db, w.boot, w.member, w.a) is False


async def test_a_fixed_scope_agent_does_not_reveal_what_is_hidden_from_the_caller(
    db_session, world
):
    w, db = world, db_session
    await _rule(db, w.boot, w.space, w.a, "deny", "user", w.member.id)
    await db.commit()
    result = await retrieve.retrieve_with_scope(
        db, qdrant=w.qdrant, organization_id=w.boot.organization_id,
        caller_user_id=w.member.id, query="q", scope_mode="fixed",
        scope_space_ids=[w.space.id], top_k=10,
    )
    names = {c.document_name for c in result.chunks}
    assert names == {"b.pdf"}  # the agent may read the space, but not what is hidden from this person


# ---------------------------------------------------------------------------
# Loosening: allow shares one document with someone who cannot read the space
# ---------------------------------------------------------------------------


async def test_a_shared_document_is_visible_and_nothing_else_in_the_space_is(db_session, world):
    w, db = world, db_session
    assert await _search_names(db, w.boot, w.qdrant, w.member) == []
    await _rule(db, w.boot, w.space, w.a, "allow", "user", w.member.id)
    await _rule(db, w.boot, w.space, w.pic, "allow", "user", w.member.id)

    assert await _search_names(db, w.boot, w.qdrant, w.member) == ["a.pdf"]
    assert await _picture_names(db, w.boot, w.qdrant, w.member) == ["site.png"]
    assert await _listed(db, w.boot, w.space, w.member) == (["a.pdf", "site.png"], 2)
    stats = await documents_service.stats_for_space(
        db, organization_id=w.boot.organization_id, space_id=w.space.id, caller_user_id=w.member.id
    )
    assert stats.total == 2
    assert await _opens(db, w.boot, w.space, w.member, w.a) is True
    assert await _opens(db, w.boot, w.space, w.member, w.b) is False  # same space, not shared
    assert await _can(db, w.boot, w.member, w.a) is True
    assert await _can(db, w.boot, w.member, w.b) is False


async def test_a_shared_document_does_not_make_the_space_writable_or_manageable(
    db_session, world
):
    w, db = world, db_session
    await _rule(db, w.boot, w.space, w.a, "allow", "user", w.member.id)
    await db.commit()
    with pytest.raises(Exception) as excinfo:
        await documents_service.upload_local(
            db, organization_id=w.boot.organization_id, space_id=w.space.id,
            uploader_user_id=w.member.id, filename="x.txt",
            file_obj=io.BytesIO(b"hi"), content_type="text/plain",
        )
    assert not isinstance(excinfo.value, TypeError)  # refused by permission, not by a bad call
    assert isinstance(excinfo.value, (NotFoundError, DocumentPermissionError))
    await db.rollback()
    with pytest.raises(NotFoundError):  # the space does not exist for managing purposes
        await rules.add_rule(
            db, organization_id=w.boot.organization_id, actor_user_id=w.member.id,
            space_id=w.space.id, document_id=w.a.id, effect="allow",
            subject_type="user", subject_id=w.member.id,
        )


async def test_the_space_lists_as_shared_only_for_that_person_and_not_for_others(
    db_session, world
):
    w, db = world, db_session
    other = await _member(db, w.boot)
    await _rule(db, w.boot, w.space, w.a, "allow", "user", w.member.id)
    await _rule(db, w.boot, w.space, w.pic, "allow", "user", w.member.id)
    await db.commit()

    plain = await spaces_service.list_spaces_for_user(
        db, organization_id=w.boot.organization_id, user_id=w.member.id
    )
    assert w.space.id not in {v.space.id for v in plain}  # never a place to write by default
    shared = await spaces_service.list_spaces_for_user(
        db, organization_id=w.boot.organization_id, user_id=w.member.id, include_shared=True
    )
    mine = next(v for v in shared if v.space.id == w.space.id)
    assert mine.shared_only_documents == 2
    assert mine.is_owner is False

    unrelated = await spaces_service.list_spaces_for_user(
        db, organization_id=w.boot.organization_id, user_id=other.id, include_shared=True
    )
    assert w.space.id not in {v.space.id for v in unrelated}

    # Being able to read the space as a whole removes the "shared only" mark.
    await _grant(db, w.boot, w.space, w.member)
    whole = await spaces_service.list_spaces_for_user(
        db, organization_id=w.boot.organization_id, user_id=w.member.id, include_shared=True
    )
    assert next(v for v in whole if v.space.id == w.space.id).shared_only_documents == 0


async def test_a_shared_only_space_can_be_named_for_reading_but_not_as_a_save_target(
    db_session, world
):
    w, db = world, db_session
    await _rule(db, w.boot, w.space, w.a, "allow", "user", w.member.id)
    await db.commit()
    view = await spaces_service.resolve_target_space(
        db, organization_id=w.boot.organization_id, user_id=w.member.id,
        name="Secret", include_shared=True,
    )
    assert view.space.id == w.space.id and view.shared_only_documents == 1
    with pytest.raises(NotFoundError):
        await spaces_service.resolve_target_space(
            db, organization_id=w.boot.organization_id, user_id=w.member.id, name="Secret"
        )


async def test_an_allow_can_name_the_whole_organisation(db_session, world):
    w, db = world, db_session
    await _rule(db, w.boot, w.space, w.a, "allow", "organization", w.boot.organization_id)
    assert await _search_names(db, w.boot, w.qdrant, w.member) == ["a.pdf"]


async def test_a_shared_document_is_dropped_when_archived_or_when_the_space_is(
    db_session, world
):
    w, db = world, db_session
    await _rule(db, w.boot, w.space, w.a, "allow", "user", w.member.id)
    assert await _search_names(db, w.boot, w.qdrant, w.member) == ["a.pdf"]

    from sqlalchemy import update

    from quberi_api.domain.spaces.models import Space

    await db.execute(update(Document).where(Document.id == w.a.id).values(status="archived"))
    await db.commit()
    assert await _search_names(db, w.boot, w.qdrant, w.member) == []

    await db.execute(update(Document).where(Document.id == w.a.id).values(status="uploaded"))
    await db.execute(update(Space).where(Space.id == w.space.id).values(status="archived"))
    await db.commit()
    assert await _search_names(db, w.boot, w.qdrant, w.member) == []


async def test_a_share_ends_with_the_rule_and_a_space_role_brings_the_rest(db_session, world):
    w, db = world, db_session
    rule = await _rule(db, w.boot, w.space, w.a, "allow", "user", w.member.id)
    assert await _search_names(db, w.boot, w.qdrant, w.member) == ["a.pdf"]
    await _unrule(db, w.boot, w.space, w.a, rule)
    assert await _search_names(db, w.boot, w.qdrant, w.member) == []
    await _grant(db, w.boot, w.space, w.member)
    assert await _search_names(db, w.boot, w.qdrant, w.member) == ["a.pdf", "b.pdf"]
    await _revoke(db, w.boot, w.space, w.member)
    assert await _search_names(db, w.boot, w.qdrant, w.member) == []


async def test_naming_a_space_narrows_a_search_without_widening_it(db_session, world):
    w, db = world, db_session
    mine = await _space(db, w.boot, "Mine")
    mine_doc = await _text_doc(db, w.boot, w.qdrant, mine, "mine.pdf")
    await _grant(db, w.boot, mine, w.member)
    await _rule(db, w.boot, w.space, w.a, "allow", "user", w.member.id)

    assert await _search_names(db, w.boot, w.qdrant, w.member) == ["a.pdf", "mine.pdf"]
    assert await _search_names(db, w.boot, w.qdrant, w.member, space_ids=[mine.id]) == ["mine.pdf"]
    assert await _search_names(db, w.boot, w.qdrant, w.member, space_ids=[w.space.id]) == ["a.pdf"]
    _ = mine_doc


# ---------------------------------------------------------------------------
# Two people, one query: nothing leaks from one to the other
# ---------------------------------------------------------------------------


async def test_two_people_with_different_rights_never_see_each_others_results(
    db_session, world
):
    w, db = world, db_session
    other = await _member(db, w.boot)
    await _grant(db, w.boot, w.space, w.member)
    await _rule(db, w.boot, w.space, w.a, "deny", "user", w.member.id)
    await _rule(db, w.boot, w.space, w.b, "allow", "user", other.id)

    for _ in range(3):  # back and forth: no answer is reused for the other person
        assert await _search_names(db, w.boot, w.qdrant, w.member) == ["b.pdf"]
        assert await _search_names(db, w.boot, w.qdrant, other) == ["b.pdf"]
        assert await _picture_names(db, w.boot, w.qdrant, w.member) == ["site.png"]
        assert await _picture_names(db, w.boot, w.qdrant, other) == []
        assert await _can(db, w.boot, w.member, w.a) is False
        assert await _can(db, w.boot, other, w.a) is False


# ---------------------------------------------------------------------------
# Managing the rules
# ---------------------------------------------------------------------------


async def test_only_a_manager_may_change_or_even_read_the_rules(db_session, world):
    w, db = world, db_session
    viewer = await _member(db, w.boot)
    editor = await _member(db, w.boot)
    admin = await _member(db, w.boot)
    stranger = await _member(db, w.boot)
    await _grant(db, w.boot, w.space, viewer, role="viewer")
    await _grant(db, w.boot, w.space, editor, role="editor")
    await _grant(db, w.boot, w.space, admin, role="admin")

    for who in (viewer, editor):
        await db.commit()
        with pytest.raises(DocumentPermissionError):
            await rules.add_rule(
                db, organization_id=w.boot.organization_id, actor_user_id=who.id,
                space_id=w.space.id, document_id=w.a.id, effect="deny",
                subject_type="user", subject_id=w.member.id,
            )
        await db.rollback()
        with pytest.raises(DocumentPermissionError):
            await rules.list_rules(
                db, organization_id=w.boot.organization_id, actor_user_id=who.id,
                space_id=w.space.id, document_id=w.a.id,
            )
        await db.rollback()
    await db.commit()
    with pytest.raises(NotFoundError):  # cannot even see the space
        await rules.list_rules(
            db, organization_id=w.boot.organization_id, actor_user_id=stranger.id,
            space_id=w.space.id, document_id=w.a.id,
        )
    await db.rollback()

    rule = await _rule(db, w.boot, w.space, w.a, "deny", "user", w.member.id, actor=admin.id)
    shown = await rules.list_rules(
        db, organization_id=w.boot.organization_id, actor_user_id=w.boot.user_id,
        space_id=w.space.id, document_id=w.a.id,
    )
    assert [(v.rule.id, v.subject_display_name) for v in shown] == [(rule.id, w.member.display_name)]
    await db.commit()
    await _unrule(db, w.boot, w.space, w.a, rule, actor=admin.id)


async def test_a_manager_who_is_denied_can_still_fix_the_rules(db_session, world):
    w, db = world, db_session
    admin = await _member(db, w.boot)
    await _grant(db, w.boot, w.space, admin, role="admin")
    rule = await _rule(db, w.boot, w.space, w.a, "deny", "user", admin.id)
    assert await _can(db, w.boot, admin, w.a) is False
    await _unrule(db, w.boot, w.space, w.a, rule, actor=admin.id)
    assert await _can(db, w.boot, admin, w.a) is True


async def test_rules_are_checked_before_they_are_stored(db_session, world):
    w, db = world, db_session
    other_org_user = uuid.uuid4()
    await db.commit()
    with pytest.raises(NotFoundError):  # a person who is not in this organisation
        await rules.add_rule(
            db, organization_id=w.boot.organization_id, actor_user_id=w.boot.user_id,
            space_id=w.space.id, document_id=w.a.id, effect="deny",
            subject_type="user", subject_id=other_org_user,
        )
    await db.rollback()
    with pytest.raises(InvalidInputError):  # "everyone" must be this organisation
        await rules.add_rule(
            db, organization_id=w.boot.organization_id, actor_user_id=w.boot.user_id,
            space_id=w.space.id, document_id=w.a.id, effect="deny",
            subject_type="organization", subject_id=uuid.uuid4(),
        )
    await db.rollback()
    with pytest.raises(InvalidInputError):
        await rules.add_rule(
            db, organization_id=w.boot.organization_id, actor_user_id=w.boot.user_id,
            space_id=w.space.id, document_id=w.a.id, effect="maybe",
            subject_type="user", subject_id=w.member.id,
        )
    await db.rollback()
    with pytest.raises(NotFoundError):  # a document that is not in that space
        await rules.add_rule(
            db, organization_id=w.boot.organization_id, actor_user_id=w.boot.user_id,
            space_id=w.boot.personal_space_id, document_id=w.a.id, effect="deny",
            subject_type="user", subject_id=w.member.id,
        )
    await db.rollback()
    await _rule(db, w.boot, w.space, w.a, "deny", "user", w.member.id)
    await db.commit()
    with pytest.raises(AlreadyExistsError):
        await rules.add_rule(
            db, organization_id=w.boot.organization_id, actor_user_id=w.boot.user_id,
            space_id=w.space.id, document_id=w.a.id, effect="deny",
            subject_type="user", subject_id=w.member.id,
        )
    await db.rollback()


async def test_every_change_is_audited_and_a_deleted_document_takes_its_rules_with_it(
    db_session, world
):
    w, db = world, db_session
    rule = await _rule(db, w.boot, w.space, w.a, "deny", "user", w.member.id)
    await _unrule(db, w.boot, w.space, w.a, rule)
    await _rule(db, w.boot, w.space, w.a, "allow", "organization", w.boot.organization_id)
    events = (
        await db.execute(
            select(AuditEvent.action, AuditEvent.payload).where(
                AuditEvent.resource_id == w.a.id, AuditEvent.action.like("document.access_rule_%")
            )
        )
    ).all()
    assert [e[0] for e in events] == [
        "document.access_rule_added",
        "document.access_rule_removed",
        "document.access_rule_added",
    ]
    assert events[0][1]["effect"] == "deny" and events[0][1]["subject_id"] == str(w.member.id)
    assert events[0][1]["display_name"] == w.member.display_name

    await db.commit()
    await documents_service.delete_document(
        db, document_id=w.a.id, organization_id=w.boot.organization_id,
        space_id=w.space.id, caller_user_id=w.boot.user_id,
    )
    left = (
        await db.execute(select(DocumentAccessRule).where(DocumentAccessRule.document_id == w.a.id))
    ).all()
    assert left == []


async def test_a_denied_person_cannot_delete_what_they_cannot_see(db_session, world):
    w, db = world, db_session
    editor = await _member(db, w.boot)
    await _grant(db, w.boot, w.space, editor, role="admin")
    await _rule(db, w.boot, w.space, w.a, "deny", "user", editor.id)
    await db.commit()
    with pytest.raises(NotFoundError):
        await documents_service.delete_document(
            db, document_id=w.a.id, organization_id=w.boot.organization_id,
            space_id=w.space.id, caller_user_id=editor.id,
        )
    await db.rollback()
    assert (await db.get(Document, w.a.id)) is not None


async def test_rules_of_another_organisation_never_apply(db_session, world):
    """A rule row is only honoured for documents of the organisation it was written in."""
    w, db = world, db_session
    db.add(
        DocumentAccessRule(
            organization_id=uuid.uuid4(),  # a different tenant
            document_id=w.a.id, effect="deny", subject_type="user", subject_id=w.member.id,
        )
    )
    with pytest.raises(Exception):  # the foreign key refuses an organisation that does not exist
        await db.flush()
    await db.rollback()


# ---------------------------------------------------------------------------
# Belt and braces: the index is only a filter, the answer is checked again
# ---------------------------------------------------------------------------


def test_the_access_decision_table():
    doc, space, other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    nothing = doc_acl.DocAccess()
    shared = doc_acl.DocAccess(granted={doc: space})
    hidden = doc_acl.DocAccess(denied=frozenset({doc}))

    assert nothing.allows(document_id=doc, space_id=space, readable_spaces={space}) is True
    assert nothing.allows(document_id=doc, space_id=space, readable_spaces=set()) is False
    assert shared.allows(document_id=doc, space_id=space, readable_spaces=set()) is True
    # A share names a document in one space; a vector that claims another space does not count.
    assert shared.allows(document_id=doc, space_id=other, readable_spaces=set()) is False
    assert hidden.allows(document_id=doc, space_id=space, readable_spaces={space}) is False
    assert hidden.allows(document_id=uuid.uuid4(), space_id=space, readable_spaces={space}) is True
    assert shared.narrowed_to([other]).granted == {}
    assert shared.narrowed_to([space]).granted == {doc: space}
    assert hidden.narrowed_to([other]).denied == {doc}  # a denial survives any narrowing
    assert shared.shared_only_counts({space}) == {}
    assert shared.shared_only_counts(set()) == {space: 1}


async def test_a_leaky_index_cannot_return_a_hidden_document(db_session, world, monkeypatch):
    """Even if the vector store ignored its filter, the answer is checked again."""
    w, db = world, db_session
    await _grant(db, w.boot, w.space, w.member)
    await _rule(db, w.boot, w.space, w.a, "deny", "user", w.member.id)
    real_text, real_image = VectorStore.search, ImageVectorStore.search_images

    async def leaky_text(self, **kw):
        kw.pop("denied_document_ids", None)
        return await real_text(self, **kw)

    async def leaky_image(self, **kw):
        kw.pop("denied_document_ids", None)
        return await real_image(self, **kw)

    monkeypatch.setattr(VectorStore, "search", leaky_text)
    monkeypatch.setattr(ImageVectorStore, "search_images", leaky_image)
    assert await _search_names(db, w.boot, w.qdrant, w.member) == ["b.pdf"]
    await _rule(db, w.boot, w.space, w.pic, "deny", "user", w.member.id)
    assert await _picture_names(db, w.boot, w.qdrant, w.member) == []


async def test_a_vector_filed_under_the_wrong_space_is_not_taken_as_a_share(
    db_session, world, qdrant
):
    """A share is for one document in its own space; a stale vector cannot carry it elsewhere."""
    w, db = world, db_session
    other_space = await _space(db, w.boot, "Elsewhere")
    await _rule(db, w.boot, w.space, w.a, "allow", "user", w.member.id)
    # The vector claims the document lives in another space.
    await _text_store(qdrant).upsert_document(
        organization_id=w.boot.organization_id, space_id=other_space.id, document_id=w.a.id,
        document_name="a.pdf", document_checksum=None,
        chunks=[ChunkPoint(chunk_index=0, text="x", vector=[1.0] + [0.0] * 7, char_start=0, char_end=1)],
    )
    assert await _search_names(db, w.boot, qdrant, w.member) == []


# ---------------------------------------------------------------------------
# An organisation admin who cannot read an enterprise space can still set access
# ---------------------------------------------------------------------------


async def _enterprise_space(db, boot):
    """An organisation-owned space created by another admin; the org owner has no binding."""
    creator = await _member(db, boot, role="admin")
    await db.commit()
    view = await spaces_service.create_collaborative_space(
        db,
        organization_id=boot.organization_id,
        creator_user_id=creator.id,
        name="Tech",
        slug=f"t-{uuid.uuid4().hex[:8]}",
        tier="team",
        owner="organization",
    )
    await db.commit()
    return SimpleNamespace(id=view.space.id), creator


async def test_an_org_admin_who_cannot_read_a_space_can_still_find_its_documents_and_set_access(
    db_session, org_and_owner, qdrant
):
    db, boot = db_session, org_and_owner
    space, _creator = await _enterprise_space(db, boot)
    doc = await _text_doc(db, boot, qdrant, space, "salaries.pdf")
    await _text_doc(db, boot, qdrant, space, "handbook.pdf")
    reader = await _member(db, boot)
    await _grant(db, boot, space, reader)

    # The org owner has no role on the space: reading is refused ...
    await db.commit()
    with pytest.raises(NotFoundError):
        await documents_service.list_documents_in_space(
            db, organization_id=boot.organization_id, space_id=space.id,
            caller_user_id=boot.user_id,
        )
    await db.rollback()
    # ... but the names are listed, with how many exceptions each has, and nothing else.
    page = await rules.list_documents_for_manager(
        db, organization_id=boot.organization_id, actor_user_id=boot.user_id, space_id=space.id
    )
    assert sorted(d.name for d in page.items) == ["handbook.pdf", "salaries.pdf"]
    assert page.total == 2
    assert {f for f in vars(page.items[0])} == {
        "id", "name", "mime_type", "size_bytes", "updated_at", "allow_rules", "deny_rules"
    }
    await db.commit()

    # They can hide a document from the reader without ever being able to read it.
    await _rule(db, boot, space, doc, "deny", "user", reader.id)
    assert await _search_names(db, boot, qdrant, reader) == ["handbook.pdf"]
    page = await rules.list_documents_for_manager(
        db, organization_id=boot.organization_id, actor_user_id=boot.user_id, space_id=space.id
    )
    assert {d.name: d.deny_rules for d in page.items} == {"salaries.pdf": 1, "handbook.pdf": 0}
    await db.commit()
    assert await _can(db, boot, SimpleNamespace(id=boot.user_id), doc) is False  # still cannot read

    # Using the listing is audited.
    actions = (
        await db.execute(
            select(AuditEvent.action).where(AuditEvent.action == "space.documents_listed_for_access")
        )
    ).scalars().all()
    assert len(actions) == 2


async def test_the_manager_listing_searches_pages_and_refuses_everyone_else(
    db_session, org_and_owner, qdrant
):
    db, boot = db_session, org_and_owner
    space, creator = await _enterprise_space(db, boot)
    for name in ("a1.pdf", "a2.pdf", "b1.pdf"):
        await _text_doc(db, boot, qdrant, space, name)
    viewer = await _member(db, boot)
    await _grant(db, boot, space, viewer)
    stranger = await _member(db, boot)

    found = await rules.list_documents_for_manager(
        db, organization_id=boot.organization_id, actor_user_id=boot.user_id,
        space_id=space.id, q="A", limit=1, offset=0,
    )
    assert found.total == 2 and len(found.items) == 1
    await db.commit()

    for who in (viewer,):
        with pytest.raises(DocumentPermissionError):
            await rules.list_documents_for_manager(
                db, organization_id=boot.organization_id, actor_user_id=who.id, space_id=space.id
            )
        await db.rollback()
    with pytest.raises(NotFoundError):
        await rules.list_documents_for_manager(
            db, organization_id=boot.organization_id, actor_user_id=stranger.id, space_id=space.id
        )
    await db.rollback()
    # The space's own creator (an owner) may.
    ok = await rules.list_documents_for_manager(
        db, organization_id=boot.organization_id, actor_user_id=creator.id, space_id=space.id
    )
    assert ok.total == 3


async def test_an_org_admin_cannot_use_it_on_someone_elses_personal_or_team_space(
    db_session, org_and_owner, qdrant
):
    """Only enterprise spaces are open to organisation admins; others stay invisible to them."""
    db, boot = db_session, org_and_owner
    owner = await _member(db, boot)
    await db.commit()
    mine = await spaces_service.create_collaborative_space(
        db, organization_id=boot.organization_id, creator_user_id=owner.id,
        name="Private", slug=f"p-{uuid.uuid4().hex[:8]}",
    )
    await db.commit()
    with pytest.raises(NotFoundError):
        await rules.list_documents_for_manager(
            db, organization_id=boot.organization_id, actor_user_id=boot.user_id,
            space_id=mine.space.id,
        )


# ---------------------------------------------------------------------------
# The owner of a space is never denied its documents
# ---------------------------------------------------------------------------


async def test_the_owner_of_a_space_cannot_be_denied_and_hiding_from_everyone_spares_them(
    db_session, world
):
    w, db = world, db_session
    boss = await _member(db, w.boot)
    await _grant(db, w.boot, w.space, boss, role="owner")
    await _grant(db, w.boot, w.space, w.member)

    # Naming the owner directly is refused, whoever asks (even the owner themselves).
    for actor in (w.owner.id, boss.id):
        await db.commit()
        with pytest.raises(InvalidInputError):
            await rules.add_rule(
                db, organization_id=w.boot.organization_id, actor_user_id=actor,
                space_id=w.space.id, document_id=w.a.id, effect="deny",
                subject_type="user", subject_id=boss.id,
            )
        await db.rollback()
    # Hiding from the whole organisation reaches everyone except the owners.
    await _rule(db, w.boot, w.space, w.a, "deny", "organization", w.boot.organization_id)
    assert await _can(db, w.boot, w.member, w.a) is False
    assert await _can(db, w.boot, boss, w.a) is True
    assert await _can(db, w.boot, w.owner, w.a) is True
    assert await _search_names(db, w.boot, w.qdrant, boss) == ["a.pdf", "b.pdf"]
    assert await _search_names(db, w.boot, w.qdrant, w.member) == ["b.pdf"]
    # A space admin who is not an owner can still be denied.
    admin = await _member(db, w.boot)
    await _grant(db, w.boot, w.space, admin, role="admin")
    await _rule(db, w.boot, w.space, w.b, "deny", "user", admin.id)
    assert await _can(db, w.boot, admin, w.b) is False


async def test_handing_the_space_over_makes_the_old_owner_deniable(db_session, world):
    from quberi_api.services import space_owners

    w, db = world, db_session
    boss = await _member(db, w.boot)
    await _grant(db, w.boot, w.space, boss, role="owner")
    successor = await _member(db, w.boot)
    await db.commit()
    await space_owners.transfer_owner(
        db, space_id=w.space.id, organization_id=w.boot.organization_id,
        actor_user_id=boss.id, new_owner_user_id=successor.id,
    )
    await db.commit()
    await _rule(db, w.boot, w.space, w.a, "deny", "user", boss.id)  # now only an editor
    assert await _can(db, w.boot, boss, w.a) is False
