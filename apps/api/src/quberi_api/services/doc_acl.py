"""Document-level read access: the one place space permissions and document rules meet.

A person can read a document when::

    (they can read its space  OR  an ``allow`` rule names them)  AND  no ``deny`` rule names them

``deny`` wins over everything, including a space role and any ``allow``. An ``allow`` lets
someone read that one document without being able to read the rest of its space.

Everything that reads documents goes through this module:

* **Search** (text and pictures) asks for a :class:`DocAccess` and hands it to the vector
  store, which filters *inside* the index: documents the person is denied are excluded,
  documents shared with them are included on top of the spaces they can read. The result
  is checked again here (:meth:`DocAccess.allows`) because the index is only a filter.
* **Lists, counts, details, downloads** build their SQL condition with :func:`visibility_clause`
  or ask :func:`can_read_document`.

Rules live in the database and are read on every request, so changing one applies to the
next request with nothing to re-index and nothing to wait out. No result is cached per
person, so there is no cached answer that could outlive a change or reach another person.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field

from sqlalchemy import and_, false, not_, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.documents.models import Document, DocumentAccessRule
from quberi_api.domain.spaces.models import Space
from quberi_api.services.authz import (
    UserPrincipals,
    list_accessible_space_ids,
    principals_for_user,
)


@dataclass(frozen=True)
class DocAccess:
    """The document-level part of one person's access, resolved from live rules.

    ``granted`` maps a document id to the space it lives in, for documents an ``allow``
    rule shares with the person (not archived, in an active space, not denied).
    ``denied`` holds the documents a ``deny`` rule hides from them. A document is never
    in both: a denied document is dropped from ``granted`` when this is built.
    """

    granted: Mapping[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    denied: frozenset[uuid.UUID] = frozenset()

    @property
    def granted_ids(self) -> list[uuid.UUID]:
        return sorted(self.granted, key=str)

    @property
    def denied_ids(self) -> list[uuid.UUID]:
        return sorted(self.denied, key=str)

    @property
    def is_empty(self) -> bool:
        return not self.granted and not self.denied

    def allows(
        self,
        *,
        document_id: uuid.UUID,
        space_id: uuid.UUID,
        readable_spaces: Collection[uuid.UUID],
    ) -> bool:
        """May this document be read, given the spaces readable as a whole?"""

        if document_id in self.denied:
            return False
        return space_id in readable_spaces or self.granted.get(document_id) == space_id

    def narrowed_to(self, space_ids: Iterable[uuid.UUID]) -> DocAccess:
        """Keep only the shared documents that live in ``space_ids`` (denials stay)."""

        keep = set(space_ids)
        return DocAccess(
            granted={d: s for d, s in self.granted.items() if s in keep},
            denied=self.denied,
        )

    def shared_only_counts(self, readable_spaces: Collection[uuid.UUID]) -> dict[uuid.UUID, int]:
        """Per space the person cannot read as a whole: how many documents are shared with them."""

        counts: dict[uuid.UUID, int] = {}
        for space_id in self.granted.values():
            if space_id not in readable_spaces:
                counts[space_id] = counts.get(space_id, 0) + 1
        return counts


NO_DOC_ACCESS = DocAccess()


def _matches(principals: UserPrincipals):
    """SQL condition: the rule names this person, directly or through a group/unit/position."""

    by_type = principals.by_type()
    return or_(
        *[
            and_(
                DocumentAccessRule.subject_type == kind,
                DocumentAccessRule.subject_id.in_(ids),
            )
            for kind, ids in by_type.items()
            if ids
        ]
    )


async def load_doc_access(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    principals: UserPrincipals | None = None,
) -> DocAccess:
    """Resolve, right now, which documents are shared with / hidden from this person."""

    principals = principals or await principals_for_user(
        session, user_id=user_id, organization_id=organization_id
    )
    rows = (
        await session.execute(
            select(
                DocumentAccessRule.effect,
                DocumentAccessRule.document_id,
                Document.space_id,
                Document.status,
                Space.status,
            )
            .join(Document, Document.id == DocumentAccessRule.document_id)
            .join(Space, Space.id == Document.space_id)
            .where(
                DocumentAccessRule.organization_id == organization_id,
                Document.organization_id == organization_id,
                _matches(principals),
            )
        )
    ).all()
    denied = {doc_id for effect, doc_id, _s, _ds, _ss in rows if effect == "deny"}
    if denied:
        # The owner of a space is never denied one of its documents: they are accountable for
        # the space's content and approve who else may read it, so no rule can hide it from
        # them, whoever it names (a person, a group, everyone). To hide a document from its
        # owner, hand the space to someone else first.
        from quberi_api.services.documents import core as documents_core

        doc_space = {doc_id: space_id for _e, doc_id, space_id, _ds, _ss in rows}
        immune_spaces: set[uuid.UUID] = set()
        for space_id in {doc_space[d] for d in denied}:
            role = await documents_core._caller_space_role(
                session, user_id=user_id, organization_id=organization_id, space_id=space_id
            )
            if role == "owner":
                immune_spaces.add(space_id)
        denied = {d for d in denied if doc_space[d] not in immune_spaces}
    granted = {
        doc_id: space_id
        for effect, doc_id, space_id, doc_status, space_status in rows
        if effect == "allow"
        and doc_id not in denied
        and doc_status != "archived"
        and space_status == "active"
    }
    return DocAccess(granted=granted, denied=frozenset(denied))


def visibility_clause(access: DocAccess, *, whole_space: bool, id_column=None):
    """SQL condition on ``documents`` for what this person may read inside one space.

    ``whole_space`` is whether they can read the space as a whole (then only denials
    subtract); otherwise only the documents shared with them are visible.
    """

    column = id_column if id_column is not None else Document.id
    parts = []
    if access.denied:
        parts.append(not_(column.in_(access.denied)))
    if not whole_space:
        parts.append(column.in_(set(access.granted)) if access.granted else false())
    return and_(*parts) if parts else true()


async def can_read_document(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    organization_id: uuid.UUID,
    document_id: uuid.UUID,
    access: DocAccess | None = None,
    readable_spaces: Collection[uuid.UUID] | None = None,
) -> bool:
    """The one question every document door asks. False for anything unknown or foreign."""

    doc = (
        await session.execute(
            select(Document.space_id).where(
                Document.id == document_id, Document.organization_id == organization_id
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        return False
    if access is None:
        access = await load_doc_access(session, user_id=user_id, organization_id=organization_id)
    if document_id in access.denied:
        return False
    if readable_spaces is None:
        readable_spaces = await list_accessible_space_ids(
            session, user_id=user_id, organization_id=organization_id
        )
    return access.allows(document_id=document_id, space_id=doc, readable_spaces=readable_spaces)
