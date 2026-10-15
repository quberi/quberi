"""Idempotent demo/sample organisation seeder.

Guests (unauthenticated visitors) browse a single, platform-maintained
*sample* organisation read-only — the "see and use before you sign in"
landing experience. This module materialises that org on boot:

* a demo org (``tier='personal'`` so the guest UI stays clean — no
  org/members chrome),
* a synthetic **guest user** the guest session maps to,
* one collaborative space the guest can *see* (via a ``viewer``
  RoleBinding — space visibility is gated by bindings, not the
  ``visibility`` column),
* a few sample documents (metadata only; not indexed),
* one sample agent (``visibility='organization'`` so any member sees it),
* one short sample conversation.

Everything is keyed off the well-known slug :data:`DEMO_ORG_SLUG`, so the
seeder is idempotent: if the org already exists it returns immediately.
A failure here must never stop API boot (the caller logs + proceeds).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.agents.models import Agent
from quberi_api.domain.authz.models import Role, RoleBinding
from quberi_api.domain.conversations.models import Conversation, Message
from quberi_api.domain.identity.models import Identity, Organization, User
from quberi_api.domain.spaces.models import Space
from quberi_api.services.demo_content import (
    DEMO_LOCALES,
    SANDBOX_SPACE_DESCRIPTION,
    SANDBOX_SPACE_NAME,
    DemoLocale,
)
from quberi_api.services.onboarding import bootstrap_organization

_log = logging.getLogger(__name__)

# Well-known identifiers. The guest-session endpoint imports these.
DEMO_ORG_SLUG = "quberi-demo"
DEMO_OWNER_EMAIL = "demo-owner@quberi.local"
DEMO_GUEST_EMAIL = "guest@quberi.local"
DEMO_AGENT_NAME = "示例助手 / Demo assistant"
DEMO_AGENT_DESCRIPTION = "演示用的通用助手 / A general assistant for the demo"


@dataclass(frozen=True)
class DemoRefs:
    """The ids a guest session needs to bind to the demo org."""

    organization_id: uuid.UUID
    guest_user_id: uuid.UUID
    guest_identity_id: uuid.UUID


async def get_demo_refs(session: AsyncSession) -> Optional[DemoRefs]:
    """Return the demo org + guest user ids, or ``None`` if not seeded yet."""

    org = (
        await session.execute(
            select(Organization).where(Organization.slug == DEMO_ORG_SLUG)
        )
    ).scalar_one_or_none()
    if org is None:
        return None
    guest = (
        await session.execute(
            select(User).where(
                User.organization_id == org.id,
                User.email == DEMO_GUEST_EMAIL,
            )
        )
    ).scalar_one_or_none()
    if guest is None:
        return None
    return DemoRefs(
        organization_id=org.id,
        guest_user_id=guest.id,
        guest_identity_id=guest.identity_id,
    )


async def _load_system_role(session: AsyncSession, code: str) -> Role:
    role = (
        await session.execute(
            select(Role)
            .where(
                Role.code == code,
                Role.is_system.is_(True),
                Role.organization_id.is_(None),
            )
            .order_by(Role.created_at.asc())
            .limit(1)
        )
    ).scalars().first()
    if role is None:
        raise RuntimeError(f"system role {code!r} not found — did migration 0002 run?")
    return role


async def ensure_demo_org(session: AsyncSession) -> DemoRefs:
    """Create the demo org + sample content if absent. Idempotent."""

    existing = await get_demo_refs(session)
    if existing is not None:
        return existing

    # The existence check above autobegins a read transaction; release it so
    # ``bootstrap_organization`` can open its own ``session.begin()`` block.
    await session.commit()

    # 1. Org + owner (+ owner personal space) via the standard bootstrap.
    #    tier='personal' keeps the guest UI free of org/members chrome.
    result = await bootstrap_organization(
        session,
        name="Quberi 示例工作区 / Demo",
        slug=DEMO_ORG_SLUG,
        owner_email=DEMO_OWNER_EMAIL,
        owner_display_name="Quberi Demo",
        tier="personal",
    )
    org_id = result.organization_id
    owner_user_id = result.user_id

    # 2. Everything else in one transaction so a partial demo never lingers.
    async with session.begin():

        # 2a. Guest identity + user (the identity guest sessions map to).
        guest_identity = Identity(
            primary_email=DEMO_GUEST_EMAIL,
            primary_email_verified=False,
            status="active",
        )
        session.add(guest_identity)
        await session.flush()

        guest_user = User(
            organization_id=org_id,
            identity_id=guest_identity.id,
            display_name="访客 Guest",
            email=DEMO_GUEST_EMAIL,
            locale="zh-CN",
            timezone="UTC",
            status="active",
            attributes={},
        )
        session.add(guest_user)
        await session.flush()

        # 2b. The sample spaces (one per interface language), their documents
        #     and the conversation guests land in are NOT created here: that
        #     needs real content stored and indexed, which ``sync_demo_content``
        #     does on every boot - for a fresh org and an already-seeded one.
        # 2d. One sample agent, visible to every member (organization).
        session.add(
            Agent(
                organization_id=org_id,
                name=DEMO_AGENT_NAME,
                description=DEMO_AGENT_DESCRIPTION,
                icon="bot",
                system_prompt="你是 Quberi 的示例助手,友好、简洁地帮助访客了解产品。",
                enabled_tools=["search_knowledge"],
                knowledge_scope_mode="user",
                scope_space_ids=[],
                visibility="organization",
                is_default=False,
                status="active",
                created_by_user_id=owner_user_id,
            )
        )

    _log.info("seeded demo organisation %s (slug=%s)", org_id, DEMO_ORG_SLUG)
    return DemoRefs(
        organization_id=org_id,
        guest_user_id=guest_user.id,
        guest_identity_id=guest_identity.id,
    )


# ---------------------------------------------------------------------------
# Keep the demo content in step with ``demo_content`` (idempotent, every boot)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DemoSyncResult:
    spaces_created: int
    conversations_reset: int


async def sync_demo_content(session: AsyncSession) -> Optional[DemoSyncResult]:
    """Make sure the demo org has its two sample spaces and welcome conversations.

    For each language there is one sample space, with the guest as a viewer and
    one conversation holding only the welcome message. Safe to run on every boot:

    * a missing space is created; an existing one (including the single
      bilingual space older deployments have) is renamed and re-tagged;
    * the sample conversation is reduced to its welcome message.

    It deliberately does **not** touch documents. They are ordinary knowledge-base
    documents, managed from the product by an owner of the demo organisation; a
    boot-time sync that overwrote them would undo whatever was uploaded there.
    Returns ``None`` when the demo org isn't seeded yet.
    """

    org = (
        await session.execute(
            select(Organization).where(Organization.slug == DEMO_ORG_SLUG)
        )
    ).scalar_one_or_none()
    owner = guest = None
    if org is not None:
        owner = (
            await session.execute(
                select(User).where(
                    User.organization_id == org.id, User.email == DEMO_OWNER_EMAIL
                )
            )
        ).scalar_one_or_none()
        guest = (
            await session.execute(
                select(User).where(
                    User.organization_id == org.id, User.email == DEMO_GUEST_EMAIL
                )
            )
        ).scalar_one_or_none()
    if org is None or owner is None or guest is None:
        await session.commit()
        return None
    org_id, owner_id, guest_id = org.id, owner.id, guest.id
    await session.commit()

    await _sync_demo_agent(session, org_id=org_id)

    spaces_created = conversations_reset = 0
    for loc in DEMO_LOCALES:
        space_id, created = await _ensure_locale_space(
            session, org_id=org_id, owner_id=owner_id, guest_id=guest_id, loc=loc
        )
        spaces_created += int(created)
        conversations_reset += int(
            await _sync_locale_conversation(
                session, org_id=org_id, space_id=space_id, guest_id=guest_id, loc=loc
            )
        )

    await _ensure_sandbox_space(
        session, org_id=org_id, owner_id=owner_id, guest_id=guest_id
    )

    _log.info(
        "demo content synced: spaces_created=%d conversations_reset=%d "
        "(documents are managed in the knowledge base, not here)",
        spaces_created,
        conversations_reset,
    )
    return DemoSyncResult(
        spaces_created=spaces_created, conversations_reset=conversations_reset
    )


async def _ensure_locale_space(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    owner_id: uuid.UUID,
    guest_id: uuid.UUID,
    loc: DemoLocale,
) -> tuple[uuid.UUID, bool]:
    """Create or refresh one language's sample space. Returns (id, created)."""

    space = (
        await session.execute(
            select(Space).where(
                Space.organization_id == org_id, Space.slug == loc.space_slug
            )
        )
    ).scalar_one_or_none()
    if space is not None:
        space_id = space.id
        await session.commit()
        async with session.begin():
            row = await session.get(Space, space_id)
            assert row is not None  # noqa: S101 - just read it
            row.name = loc.space_name
            row.description = loc.space_description
            row.attributes = {**(row.attributes or {}), "locale": loc.locale, "demo": True}
            row.status = "active"
    else:
        await session.commit()
        async with session.begin():
            owner_role = await _load_system_role(session, "owner")
            new = Space(
                organization_id=org_id,
                name=loc.space_name,
                slug=loc.space_slug,
                description=loc.space_description,
                preset="collaborative",
                owner_type="user",
                owner_id=owner_id,
                visibility="organization",
                trust_level="guided",
                inheritance_policy={},
                attributes={"locale": loc.locale, "demo": True},
                status="active",
            )
            session.add(new)
            await session.flush()
            space_id = new.id
            session.add(
                RoleBinding(
                    organization_id=org_id,
                    role_id=owner_role.id,
                    subject_type="user",
                    subject_id=owner_id,
                    resource_type="space",
                    resource_id=space_id,
                    conditions={},
                )
            )

    # The guest must be able to see the space, and only read it.
    existing_binding = (
        await session.execute(
            select(RoleBinding.id).where(
                RoleBinding.organization_id == org_id,
                RoleBinding.resource_type == "space",
                RoleBinding.resource_id == space_id,
                RoleBinding.subject_type == "user",
                RoleBinding.subject_id == guest_id,
            )
        )
    ).first()
    await session.commit()
    if existing_binding is None:
        async with session.begin():
            viewer_role = await _load_system_role(session, "viewer")
            session.add(
                RoleBinding(
                    organization_id=org_id,
                    role_id=viewer_role.id,
                    subject_type="user",
                    subject_id=guest_id,
                    resource_type="space",
                    resource_id=space_id,
                    conditions={},
                )
            )
    return space_id, space is None


async def _sync_locale_conversation(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    space_id: uuid.UUID,
    guest_id: uuid.UUID,
    loc: DemoLocale,
) -> bool:
    """Make the space's one conversation hold only the welcome message.

    Creates it if missing. Returns True when anything was changed.
    """

    from sqlalchemy import delete

    conv = (
        await session.execute(
            select(Conversation)
            .where(
                Conversation.organization_id == org_id,
                Conversation.space_id == space_id,
            )
            .order_by(Conversation.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    msgs: list = []
    conv_id = None
    if conv is not None:
        conv_id = conv.id
        msgs = list(
            (
                await session.execute(
                    select(Message)
                    .where(Message.conversation_id == conv.id)
                    .order_by(Message.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
    up_to_date = (
        conv is not None
        and conv.title == loc.conversation_title
        and len(msgs) == 1
        and msgs[0].role == "assistant"
        and (msgs[0].content or {}).get("text") == loc.welcome
    )
    await session.commit()
    if up_to_date:
        return False

    async with session.begin():
        if conv_id is None:
            new = Conversation(
                organization_id=org_id,
                space_id=space_id,
                created_by_user_id=guest_id,
                title=loc.conversation_title,
                status="active",
                scope_mode="auto",
                message_count=1,
                attributes={"demo": True, "locale": loc.locale},
            )
            session.add(new)
            await session.flush()
            conv_id = new.id
        else:
            await session.execute(
                delete(Message).where(Message.conversation_id == conv_id)
            )
            row = await session.get(Conversation, conv_id)
            if row is not None:
                row.title = loc.conversation_title
                row.message_count = 1
                row.attributes = {
                    **(row.attributes or {}),
                    "demo": True,
                    "locale": loc.locale,
                }
        session.add(
            Message(
                conversation_id=conv_id,
                organization_id=org_id,
                role="assistant",
                content={"type": "text", "text": loc.welcome},
                author_user_id=None,
                tokens={},
                cost={},
                status="completed",
            )
        )
    return True


async def _sync_demo_agent(session: AsyncSession, *, org_id: uuid.UUID) -> None:
    """Keep the sample assistant's visible text current on existing deployments.

    ``ensure_demo_org`` only writes the agent the first time, so wording fixes
    (like the bilingual description the UI now splits by language) would never
    reach an org that was seeded earlier.
    """

    # ``.first()``: a duplicate must never abort the sync - a failure here
    # would skip the spaces and documents that follow.
    agent = (
        await session.execute(
            select(Agent)
            .where(
                Agent.organization_id == org_id,
                Agent.name == DEMO_AGENT_NAME,
            )
            .order_by(Agent.created_at.asc())
            .limit(1)
        )
    ).scalars().first()
    if agent is None or agent.description == DEMO_AGENT_DESCRIPTION:
        await session.commit()
        return
    agent_id = agent.id
    await session.commit()
    async with session.begin():
        row = await session.get(Agent, agent_id)
        if row is not None:
            row.description = DEMO_AGENT_DESCRIPTION


async def _ensure_sandbox_space(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    owner_id: uuid.UUID,
    guest_id: uuid.UUID,
) -> uuid.UUID:
    """The shared "Guest sandbox" where an anonymous visitor may try uploading.

    One space for both languages. The guest is an **editor** here (the product
    spaces give them viewer only) so the dedicated guest-upload endpoint can
    reuse the normal upload service; the global guest read-only guard still
    blocks every ordinary write endpoint, so this grants nothing by itself.
    Documents are never created here - guests upload them, and they expire.
    """

    from quberi_api.services.guest_sandbox import SANDBOX_SLUG

    space = (
        await session.execute(
            select(Space).where(
                Space.organization_id == org_id, Space.slug == SANDBOX_SLUG
            )
        )
    ).scalar_one_or_none()
    if space is not None:
        space_id = space.id
        await session.commit()
        async with session.begin():
            row = await session.get(Space, space_id)
            assert row is not None  # noqa: S101 - just read it
            row.name = SANDBOX_SPACE_NAME
            row.description = SANDBOX_SPACE_DESCRIPTION
            row.attributes = {**(row.attributes or {}), "demo": True, "sandbox": True}
            row.status = "active"
    else:
        await session.commit()
        async with session.begin():
            owner_role = await _load_system_role(session, "owner")
            new = Space(
                organization_id=org_id,
                name=SANDBOX_SPACE_NAME,
                slug=SANDBOX_SLUG,
                description=SANDBOX_SPACE_DESCRIPTION,
                preset="collaborative",
                owner_type="user",
                owner_id=owner_id,
                visibility="organization",
                trust_level="exploratory",
                inheritance_policy={},
                attributes={"demo": True, "sandbox": True},
                status="active",
            )
            session.add(new)
            await session.flush()
            space_id = new.id
            session.add(
                RoleBinding(
                    organization_id=org_id,
                    role_id=owner_role.id,
                    subject_type="user",
                    subject_id=owner_id,
                    resource_type="space",
                    resource_id=space_id,
                    conditions={},
                )
            )

    has_editor = (
        await session.execute(
            select(RoleBinding.id)
            .join(Role, Role.id == RoleBinding.role_id)
            .where(
                RoleBinding.organization_id == org_id,
                RoleBinding.resource_type == "space",
                RoleBinding.resource_id == space_id,
                RoleBinding.subject_type == "user",
                RoleBinding.subject_id == guest_id,
                Role.code == "editor",
            )
        )
    ).first()
    await session.commit()
    if has_editor is None:
        async with session.begin():
            editor_role = await _load_system_role(session, "editor")
            session.add(
                RoleBinding(
                    organization_id=org_id,
                    role_id=editor_role.id,
                    subject_type="user",
                    subject_id=guest_id,
                    resource_type="space",
                    resource_id=space_id,
                    conditions={},
                )
            )
    return space_id
