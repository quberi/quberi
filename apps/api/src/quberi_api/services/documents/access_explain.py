"""Why can (or can't) this person read this document?

An administrator looking at one document and one person gets the answer and the
reasons behind it: the space role that lets them in (and through which path: directly,
a group, a department, a position), a rule that shares the document with them, a rule
that hides it, an account that is switched off, a file still waiting for approval.

The verdict is not worked out a second time here. It comes from the same functions the
document doors use (``_caller_space_role``, ``doc_acl.load_doc_access`` and the review
scope), so the explanation can never disagree with what the person actually sees. Only
the reasons are gathered separately, and the tests check that the two always agree.

Only someone who manages the document's space (a space admin or owner, or an organisation
owner or admin) may ask, and it reads nothing but names and rules: no document text.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.documents.models import DocumentAccessRule
from quberi_api.domain.enums import READABLE_SPACE_STATUSES
from quberi_api.domain.identity.models import User
from quberi_api.domain.spaces.models import Space
from quberi_api.services import doc_acl, space_bindings
from quberi_api.services.authz import (
    principals_for_user,
    resolve_effective_role_bindings,
    user_is_blocked,
)
from quberi_api.services.documents import access_rules
from quberi_api.services.documents import core as documents_core
from quberi_api.services.documents import review as review_service
from quberi_api.services.errors import InvalidInputError, NotFoundError

# What happened, in order of how decisive it is.
OUTCOMES = (
    "account_blocked",
    "denied_by_rule",
    "waiting_for_approval",
    "allowed_by_space",
    "allowed_by_share",
    "no_access",
)

_HIT_PATH_TEXT = {
    "direct": ("直接授权给这个人", "granted to them directly"),
    "position": ("通过岗位", "through their position"),
    "organization_unit": ("通过所在部门", "through their department"),
    "organization_unit_ancestor": ("通过上级部门", "through a department above theirs"),
    "group_via_user": ("通过用户组", "through a group they belong to"),
    "group_via_position": ("通过用户组（按岗位加入）", "through a group (joined by position)"),
    "group_via_organization_unit": (
        "通过用户组（按部门加入）",
        "through a group (joined by department)",
    ),
    "organization": ("整个组织都有", "given to the whole organisation"),
}
_SUBJECT_TEXT = {
    "user": ("这个人", "this person"),
    "group": ("用户组", "the group"),
    "organization_unit": ("部门", "the department"),
    "position": ("岗位", "the position"),
    "organization": ("整个组织", "the whole organisation"),
}


def _both(zh: str, en: str) -> str:
    return f"{zh} {en}"


@dataclass(frozen=True)
class ExplainStep:
    """One reason. ``effect`` says what it does to the answer."""

    kind: str  # account | space | space_role | space_owner | allow_rule | deny_rule | review | document
    effect: str  # grants | blocks | ignored | info
    code: str  # short and stable, for a screen to localise
    summary: str  # a sentence for a person reading the raw answer (Chinese and English)
    subject_type: Optional[str] = None
    subject_id: Optional[uuid.UUID] = None
    subject_display_name: Optional[str] = None
    role_code: Optional[str] = None
    hit_path: Optional[str] = None
    rule_id: Optional[uuid.UUID] = None


@dataclass(frozen=True)
class AccessExplanation:
    user_id: uuid.UUID
    user_display_name: str
    user_email: str
    user_status: str
    document_id: uuid.UUID
    document_name: str
    space_id: uuid.UUID
    space_name: str
    review_state: str
    indexing_state: str
    can_read: bool
    outcome: str
    # Their space role (viewer, editor, admin, owner), or None when they have none.
    space_role: Optional[str]
    # Whether the document can turn up in this person's search results.
    appears_in_search: bool
    steps: tuple[ExplainStep, ...]


async def explain_document_access(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    space_id: uuid.UUID,
    document_id: uuid.UUID,
    subject_user_id: uuid.UUID,
) -> AccessExplanation:
    # 404 for anything the actor cannot manage; 403 when they can see but not manage.
    doc = await access_rules._manageable_document(
        session,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        space_id=space_id,
        document_id=document_id,
    )
    person = (
        await session.execute(
            select(User).where(User.id == subject_user_id, User.organization_id == organization_id)
        )
    ).scalar_one_or_none()
    if person is None:
        raise NotFoundError(f"member not found: {subject_user_id}")
    space = (
        await session.execute(
            select(Space).where(Space.id == space_id, Space.organization_id == organization_id)
        )
    ).scalar_one()

    # ---- the verdict, from the same functions the document doors use ----------------
    blocked = await user_is_blocked(session, user_id=person.id)
    role = await documents_core._caller_space_role(
        session, user_id=person.id, organization_id=organization_id, space_id=space_id
    )
    whole_space = documents_core._at_least(role, "viewer")
    access = await doc_acl.load_doc_access(
        session, user_id=person.id, organization_id=organization_id
    )
    review_scope = review_service.review_scope_for_role(person.id, role)

    denied = doc.id in access.denied
    shared = doc.id in access.granted
    review_ok = review_scope.allows(doc)
    can_read = (not blocked) and (not denied) and (whole_space or shared) and review_ok

    if blocked:
        outcome = "account_blocked"
    elif denied:
        outcome = "denied_by_rule"
    elif not (whole_space or shared):
        outcome = "no_access"
    elif not review_ok:
        outcome = "waiting_for_approval"
    else:
        outcome = "allowed_by_space" if whole_space else "allowed_by_share"

    appears_in_search = (
        can_read
        and doc.review_state == "approved"
        and doc.indexing_state == "indexed"
        and doc.status != "archived"
    )

    # ---- the reasons --------------------------------------------------------------------
    steps: list[ExplainStep] = []

    if blocked:
        steps.append(
            ExplainStep(
                kind="account",
                effect="blocks",
                code="account_blocked",
                summary=_both(
                    "这个人的账号已停用或归档，什么都看不到，任何授权都不起作用。",
                    "Their account is suspended or archived, so they can read nothing and no "
                    "grant or rule has any effect.",
                ),
            )
        )

    if space.status not in READABLE_SPACE_STATUSES:
        steps.append(
            ExplainStep(
                kind="space",
                effect="blocks",
                code="space_unavailable",
                summary=_both(
                    f"空间当前状态是「{space.status}」，任何人都读不了其中的内容。",
                    f"The space is {space.status}, so nobody can read what is in it.",
                ),
            )
        )
    elif space.status == "archived":
        steps.append(
            ExplainStep(
                kind="space",
                effect="info",
                code="space_archived",
                summary=_both("这个空间已归档。", "The space is archived."),
            )
        )

    # Space roles, with the path that applies each one.
    bindings = (
        []
        if blocked
        else await resolve_effective_role_bindings(
            session,
            user_id=person.id,
            organization_id=organization_id,
            resource_type="space",
            resource_id=space_id,
            user_blocked=False,
        )
    )
    for hit in sorted(bindings, key=lambda b: b.role_code):
        name = await _subject_name(
            session, hit.binding.subject_type, hit.binding.subject_id, organization_id
        )
        zh, en = _HIT_PATH_TEXT.get(hit.hit_path, (hit.hit_path, hit.hit_path))
        steps.append(
            ExplainStep(
                kind="space_role",
                effect="grants" if hit.role_code in _READ_ROLES else "info",
                code="space_role",
                summary=_both(
                    f"空间角色「{hit.role_code}」，{zh}（{name}）。",
                    f"Space role '{hit.role_code}', {en} ({name}).",
                ),
                subject_type=hit.binding.subject_type,
                subject_id=hit.binding.subject_id,
                subject_display_name=name,
                role_code=hit.role_code,
                hit_path=hit.hit_path,
            )
        )
    top = max(
        (b.role_code for b in bindings), key=lambda c: documents_core._ROLE_STRENGTH.get(c, -1),
        default=None,
    )
    if role is not None and top is not None and role != top:
        steps.append(
            ExplainStep(
                kind="space_role",
                effect="info",
                code="role_capped",
                summary=_both(
                    f"实际生效的角色是「{role}」，低于上面最高的「{top}」"
                    "（团队或企业组织的组织所有者不是空间管理员）。",
                    f"The role that applies is '{role}', lower than the highest above ('{top}'): "
                    "the organisation owner of a team or enterprise organisation is not a space "
                    "administrator.",
                ),
                role_code=role,
            )
        )
    if (not blocked) and space.owner_type == "user" and space.owner_id == person.id:
        steps.append(
            ExplainStep(
                kind="space_owner",
                effect="grants",
                code="space_owner",
                summary=_both("这是这个人自己的空间。", "This is their own space."),
                role_code="owner",
            )
        )
    if not blocked and not bindings and not (
        space.owner_type == "user" and space.owner_id == person.id
    ):
        steps.append(
            ExplainStep(
                kind="space_role",
                effect="info",
                code="no_space_role",
                summary=_both(
                    "没有任何空间角色能让这个人读到这个空间。",
                    "Nothing gives them a role on this space.",
                ),
            )
        )

    # Rules on this document that name this person.
    principals = await principals_for_user(
        session, user_id=person.id, organization_id=organization_id
    )
    by_type = principals.by_type()
    rules = (
        (
            await session.execute(
                select(DocumentAccessRule)
                .where(
                    DocumentAccessRule.document_id == doc.id,
                    DocumentAccessRule.organization_id == organization_id,
                )
                .order_by(DocumentAccessRule.effect, DocumentAccessRule.created_at)
            )
        )
        .scalars()
        .all()
    )
    for rule in rules:
        if rule.subject_id not in by_type.get(rule.subject_type, frozenset()):
            continue
        name = await _subject_name(session, rule.subject_type, rule.subject_id, organization_id)
        zh_subject, en_subject = _SUBJECT_TEXT.get(rule.subject_type, (rule.subject_type,) * 2)
        if rule.effect == "deny":
            owner_immune = role == "owner"
            steps.append(
                ExplainStep(
                    kind="deny_rule",
                    effect="ignored" if owner_immune else "blocks",
                    code="deny_ignored_for_owner" if owner_immune else "deny_rule",
                    summary=(
                        _both(
                            f"有一条「拒绝」规则指向{zh_subject}（{name}），但空间的所有者不会被拒绝，"
                            "所以这条不起作用。",
                            f"A deny rule names {en_subject} ({name}), but the owner of a space is "
                            "never denied its documents, so it has no effect.",
                        )
                        if owner_immune
                        else _both(
                            f"有一条「拒绝」规则指向{zh_subject}（{name}）。拒绝优先于一切，"
                            "包括空间角色和任何「允许」。",
                            f"A deny rule names {en_subject} ({name}). Deny wins over everything, "
                            "including a space role and any allow.",
                        )
                    ),
                    subject_type=rule.subject_type,
                    subject_id=rule.subject_id,
                    subject_display_name=name,
                    rule_id=rule.id,
                )
            )
        else:
            usable = (
                not blocked
                and doc.status != "archived"
                and doc.review_state == "approved"
                and space.status == "active"
            )
            steps.append(
                ExplainStep(
                    kind="allow_rule",
                    effect="grants" if usable and not denied else "info",
                    code="allow_rule",
                    summary=_both(
                        f"有一条「允许」规则指向{zh_subject}（{name}），让这个人能读这一份文档，"
                        "不需要能读整个空间。",
                        f"An allow rule names {en_subject} ({name}), so they can read this one "
                        "document without reading the whole space.",
                    ),
                    subject_type=rule.subject_type,
                    subject_id=rule.subject_id,
                    subject_display_name=name,
                    rule_id=rule.id,
                )
            )

    # The file's own state.
    if doc.review_state != "approved":
        steps.append(
            ExplainStep(
                kind="review",
                effect="info" if review_ok else "blocks",
                code=f"review_{doc.review_state}",
                summary=_both(
                    f"这份文档{'正在等待审核' if doc.review_state == 'pending' else '已被驳回'}，"
                    "只有上传者和审核人能看到，也不会被搜索到。",
                    f"The document is {'waiting for approval' if doc.review_state == 'pending' else 'rejected'}; "
                    "only the uploader and reviewers can see it and it is not searchable.",
                ),
            )
        )
    if doc.status == "archived":
        steps.append(
            ExplainStep(
                kind="document",
                effect="info",
                code="document_archived",
                summary=_both("这份文档已归档。", "The document is archived."),
            )
        )
    if can_read and not appears_in_search:
        steps.append(
            ExplainStep(
                kind="document",
                effect="info",
                code="not_searchable_yet",
                summary=_both(
                    "能读，但现在搜不到：文档还没有完成索引。",
                    "They can read it, but it is not searchable yet: indexing is not finished.",
                ),
            )
        )

    session.add(
        AuditEvent(
            organization_id=organization_id,
            actor_type="user",
            actor_id=actor_user_id,
            action="document.access_checked",
            resource_type="document",
            resource_id=doc.id,
            space_id=space_id,
            payload={"subject_user_id": str(person.id), "outcome": outcome},
        )
    )
    await session.commit()

    return AccessExplanation(
        user_id=person.id,
        user_display_name=person.display_name,
        user_email=person.email,
        user_status=person.status,
        document_id=doc.id,
        document_name=doc.name,
        space_id=space.id,
        space_name=space.name,
        review_state=doc.review_state,
        indexing_state=doc.indexing_state,
        can_read=can_read,
        outcome=outcome,
        space_role=role,
        appears_in_search=appears_in_search,
        steps=tuple(steps),
    )


# Roles that let someone read a space as a whole ("member" is not assignable on a space and
# does not count, exactly as in ``documents_core._at_least``).
_READ_ROLES = frozenset({"viewer", "editor", "admin", "owner"})


async def _subject_name(
    session: AsyncSession, subject_type: str, subject_id: uuid.UUID, organization_id: uuid.UUID
) -> str:
    try:
        name, _subtitle = await space_bindings.resolve_subject_display(
            session,
            subject_type=subject_type,
            subject_id=subject_id,
            organization_id=organization_id,
        )
    except (NotFoundError, InvalidInputError):
        return "(removed)"
    return name
