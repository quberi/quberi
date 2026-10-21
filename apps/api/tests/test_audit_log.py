"""The audit log: who may read it, what it returns, and that it pages cleanly.

The log records who asked what and which documents the assistant could see,
so reading it is limited to organisation owners and admins, document names
are looked up only inside the caller's own organisation, and the CSV export
cannot be turned into a spreadsheet formula by a name someone typed.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from quberi_api.api.security import CallerContext
from quberi_api.api.v1.organizations import audit as audit_api
from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.documents.models import Document
from quberi_api.domain.identity.models import Organization, User
from quberi_api.services import audit as audit_service
from quberi_api.services import document_sources as sources_service
from quberi_api.services import members as members_service
from quberi_api.services import onboarding
from quberi_api.services import spaces as spaces_service
from quberi_api.services.errors import InvalidInputError

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)


async def _other_org(db):
    await db.commit()
    unique = uuid.uuid4().hex[:8]
    return await onboarding.bootstrap_organization(
        db,
        name=f"Other {unique}",
        slug=f"other-{unique}",
        owner_email=f"o-{unique}@example.com",
        owner_display_name="Other Owner",
        tier="team",
    )


async def _caller(db, org_boot, user):
    organization = await db.get(Organization, org_boot.organization_id)
    return CallerContext(user=user, organization=organization, is_platform_admin=False)


async def _owner(db, org_boot):
    return await _caller(db, org_boot, await db.get(User, org_boot.user_id))


async def _invite(db, org, role_code, display_name=None):
    await db.commit()
    view = await members_service.invite_member(
        db,
        organization_id=org.organization_id,
        invited_by_user_id=org.user_id,
        email=f"{role_code}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=display_name or role_code.title(),
        role_code=role_code,
    )
    return view.user


def _event(org_id, *, action="space.created", minutes=0, actor_id=None, space_id=None,
           payload=None, created_at=None, resource_type="space", resource_id=None):
    return AuditEvent(
        organization_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        space_id=space_id,
        payload=payload or {},
        created_at=created_at or (T0 + timedelta(minutes=minutes)),
    )


async def _add(db, *events):
    await db.commit()
    for e in events:
        db.add(e)
    await db.flush()
    await db.commit()


# ---------------------------------------------------------------------------
# Who may read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ordinary_members_cannot_read_the_log_in_any_form(db_session, org_and_owner):
    member = await _invite(db_session, org_and_owner, "member")
    caller = await _caller(db_session, org_and_owner, member)
    calls = (
        audit_api.list_recent_events(limit=5, caller=caller, session=db_session),
        audit_api.search_audit_events(
            limit=5, cursor=None, action=None, actor_id=None, space_id=None,
            since=None, until=None, caller=caller, session=db_session,
        ),
        audit_api.list_audit_actions(caller=caller, session=db_session),
        audit_api.export_audit_events(
            action=None, actor_id=None, space_id=None, since=None, until=None,
            caller=caller, session=db_session,
        ),
    )
    for call in calls:
        with pytest.raises(HTTPException) as exc:
            await call
        assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_owner_and_admin_can_read_the_log(db_session, org_and_owner):
    await _add(db_session, _event(org_and_owner.organization_id))
    admin = await _invite(db_session, org_and_owner, "admin")
    for caller in (
        await _owner(db_session, org_and_owner),
        await _caller(db_session, org_and_owner, admin),
    ):
        page = await audit_api.search_audit_events(
            limit=10, cursor=None, action=None, actor_id=None, space_id=None,
            since=None, until=None, caller=caller, session=db_session,
        )
        assert any(i.action == "space.created" for i in page.items)


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filters_by_action_actor_space_and_time(db_session, org_and_owner):
    org_id = org_and_owner.organization_id
    alice = await _invite(db_session, org_and_owner, "member", "Alice")
    space = (await spaces_service.create_collaborative_space(
        db_session, organization_id=org_id, creator_user_id=org_and_owner.user_id,
        name="S", slug=f"s-{uuid.uuid4().hex[:6]}")).space
    await _add(
        db_session,
        _event(org_id, action="message.sent", minutes=1, actor_id=alice.id, space_id=space.id),
        _event(org_id, action="document.uploaded", minutes=2, actor_id=alice.id),
        _event(org_id, action="message.sent", minutes=3, actor_id=org_and_owner.user_id),
        _event(org_id, action="space.created", minutes=4, actor_id=org_and_owner.user_id),
    )

    async def q(**kw):
        page = await audit_service.search_events(
            db_session, organization_id=org_id, limit=100, **kw
        )
        return [(e.action, e.created_at) for e in page.items]

    assert [a for a, _ in await q(action="message.sent")] == ["message.sent", "message.sent"]
    assert {a for a, _ in await q(actor_id=alice.id)} == {"message.sent", "document.uploaded"}
    # Creating the space is itself an event in that space, next to our message.
    in_space = {a for a, _ in await q(space_id=space.id)}
    assert in_space == {"space.created", "message.sent"}
    got = await q(since=T0 + timedelta(minutes=2), until=T0 + timedelta(minutes=4))
    assert [a for a, _ in got] == ["message.sent", "document.uploaded"]  # until is exclusive
    # Newest first.
    times = [t for _, t in await q()]
    assert times == sorted(times, reverse=True)


@pytest.mark.asyncio
async def test_other_organizations_events_are_never_returned(db_session, org_and_owner):
    other = await _other_org(db_session)
    await _add(
        db_session,
        _event(org_and_owner.organization_id, action="mine.event"),
        _event(other.organization_id, action="theirs.event"),
    )
    page = await audit_service.search_events(
        db_session, organization_id=org_and_owner.organization_id, limit=100
    )
    assert "theirs.event" not in {e.action for e in page.items}
    assert "mine.event" in {e.action for e in page.items}
    actions = await audit_service.list_actions(
        db_session, organization_id=org_and_owner.organization_id
    )
    assert "theirs.event" not in actions and "mine.event" in actions
    assert actions == sorted(set(actions))


# ---------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cursor_paging_returns_every_event_once_including_equal_timestamps(
    db_session, org_and_owner
):
    org_id = org_and_owner.organization_id
    events = [_event(org_id, action=f"a.{i}", minutes=i) for i in range(4)]
    # Four more with the *same* timestamp: only the id can order them.
    events += [_event(org_id, action=f"tie.{i}", created_at=T0 + timedelta(hours=1))
               for i in range(4)]
    await _add(db_session, *events)

    seen: list[uuid.UUID] = []
    cursor = None
    pages = 0
    while True:
        page = await audit_service.search_events(
            db_session, organization_id=org_id, limit=3, cursor=cursor
        )
        seen += [e.id for e in page.items]
        pages += 1
        cursor = page.next_cursor
        if cursor is None:
            break
        assert pages < 10
    assert len(seen) == len(set(seen))  # nothing repeated
    # Everything we added is present (the fixture may add a few of its own).
    assert {e.id for e in events} <= set(seen)
    assert pages >= 3  # 8+ events at 3 per page really did span several pages


@pytest.mark.asyncio
async def test_a_tampered_cursor_is_rejected(db_session, org_and_owner):
    with pytest.raises(InvalidInputError):
        await audit_service.search_events(
            db_session, organization_id=org_and_owner.organization_id, cursor="not-a-cursor"
        )


# ---------------------------------------------------------------------------
# Retrieved documents
# ---------------------------------------------------------------------------


async def _doc(db, org_boot, space, name):
    local = await sources_service.ensure_local_source(db, organization_id=org_boot.organization_id)
    await db.commit()
    d = Document(
        organization_id=org_boot.organization_id, space_id=space.id, source_id=local.id,
        name=name, mime_type="text/plain", size_bytes=1, checksum_sha256=uuid.uuid4().hex * 2,
        external_ref={},
    )
    db.add(d)
    await db.flush()
    await db.commit()
    return d


@pytest.mark.asyncio
async def test_retrieved_document_ids_show_as_names_scoped_to_the_organization(
    db_session, org_and_owner
):
    org_id = org_and_owner.organization_id
    space = (await spaces_service.create_collaborative_space(
        db_session, organization_id=org_id, creator_user_id=org_and_owner.user_id,
        name="S", slug=f"s-{uuid.uuid4().hex[:6]}")).space
    mine = await _doc(db_session, org_and_owner, space, "handbook.docx")
    other = await _other_org(db_session)
    other_space = (await spaces_service.create_collaborative_space(
        db_session, organization_id=other.organization_id, creator_user_id=other.user_id,
        name="T", slug=f"t-{uuid.uuid4().hex[:6]}")).space
    theirs = await _doc(db_session, other, other_space, "secret-of-another-company.docx")
    gone = uuid.uuid4()  # deleted, or never existed
    await _add(
        db_session,
        _event(
            org_id, action="message.sent",
            payload={"retrieved_document_ids": [str(mine.id), str(theirs.id), str(gone), "junk"]},
        ),
    )
    page = await audit_service.search_events(
        db_session, organization_id=org_id, limit=5, action="message.sent"
    )
    docs = {d["id"]: d["name"] for d in page.items[0].retrieved_documents}
    assert docs[str(mine.id)] == "handbook.docx"
    # Another organisation's document id never reveals its name.
    assert docs[str(theirs.id)] is None
    assert docs[str(gone)] is None
    assert "junk" not in docs  # not an id: skipped, not an error


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_csv_export_neutralizes_spreadsheet_formulas(db_session, org_and_owner):
    org_id = org_and_owner.organization_id
    evil = await _invite(db_session, org_and_owner, "member", "=HYPERLINK(\"http://evil\",\"x\")")
    await _add(
        db_session,
        _event(org_id, action="message.sent", actor_id=evil.id, payload={"note": "=1+1"}),
    )
    owner = await _owner(db_session, org_and_owner)
    resp = await audit_api.export_audit_events(
        action="message.sent", actor_id=None, space_id=None, since=None, until=None,
        caller=owner, session=db_session,
    )
    assert resp.media_type.startswith("text/csv")
    text = resp.body.decode("utf-8")
    assert text.startswith("\ufeff")
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    header, body = rows[0], rows[1:]
    assert header[:4] == ["time", "actor", "actor_id", "action"]
    actors = [r[1] for r in body]
    assert any(a.startswith("'=HYPERLINK") for a in actors)  # prefixed, not executable
    assert not any(a.startswith("=") for a in actors)


def test_csv_safe_prefixes_every_formula_starter():
    for s in ("=1", "+1", "-1", "@SUM", "\tx", "\rx"):
        assert audit_api.csv_safe(s).startswith("'")
    assert audit_api.csv_safe("plain") == "plain"
    assert audit_api.csv_safe(None) == ""
