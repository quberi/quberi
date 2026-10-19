"""Credentials that must not outlive a member: Feishu grants and private model keys.

Both functions run inside the caller's transaction and never commit, so deactivating a
member removes these together with everything else in one commit (or not at all).
Nothing here logs or returns a key, a token or a ciphertext: only ids and counts.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.ai.models import AiModelSelection, AiProviderBinding
from quberi_api.domain.audit.models import AuditEvent
from quberi_api.domain.documents.models import DocumentSource
from quberi_api.domain.identity.models import User
from quberi_api.domain.integrations.models import (
    IntegrationOAuthState,
    IntegrationUserToken,
)
from quberi_api.services.errors import OffboardingRequestError

PAUSED_MEMBER_DEACTIVATED = "member_deactivated"


@dataclass(frozen=True)
class PrivateModelClearResult:
    bindings_removed: int
    """Private bindings whose key is gone (deleted, or kept as an emptied archived row)."""
    org_selections_removed: int
    """This organisation's chat selections that pointed at one of the keys (deleted)."""
    other_org_selections_invalidated: int
    """Other organisations' selections that pointed at one of the keys (kept, audited)."""


async def clear_feishu(
    session: AsyncSession, *, user: User, actor_user_id: uuid.UUID
) -> tuple[int, int]:
    """Drop the member's Feishu grants and stop the syncs they connected.

    Returns ``(feishu_tokens_removed, feishu_sources_paused)``.

    Syncing uses the platform's Feishu app, not the member's personal token, so deleting
    the token alone would not stop a connected folder. The member is recorded as the one
    who connected it (``created_by_user_id``), so those sources are paused. Sources
    somebody else connected are left running. Documents already synced stay where they are.
    Restoring the member does not resume a paused source: reconnecting is a decision for
    someone who can open the folder with their own Feishu authorisation.
    """

    removed = (
        await session.execute(
            delete(IntegrationUserToken)
            .where(IntegrationUserToken.user_id == user.id)
            .returning(IntegrationUserToken.id)
        )
    ).all()
    await session.execute(
        delete(IntegrationOAuthState).where(IntegrationOAuthState.user_id == user.id)
    )
    sources = (
        (
            await session.execute(
                select(DocumentSource)
                .where(
                    DocumentSource.kind == "feishu",
                    DocumentSource.created_by_user_id == user.id,
                    DocumentSource.status == "active",
                    DocumentSource.organization_id == user.organization_id,
                )
                .order_by(DocumentSource.created_at, DocumentSource.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for source in sources:
        source.status = "paused"
        # A new dict: in-place edits to a JSONB value are not tracked.
        source.config = {
            **(source.config or {}),
            "paused_reason": PAUSED_MEMBER_DEACTIVATED,
            "paused_member_id": str(user.id),
        }
    return len(removed), len(sources)


async def clear_private_models(
    session: AsyncSession, *, user: User, actor_user_id: uuid.UUID
) -> PrivateModelClearResult:
    """Remove the member's own (BYOK) model keys; none of them is kept.

    Private bindings belong to the person's global identity, so this reaches every
    private binding that identity has, in any organisation. Organisation-level bindings
    are not touched, whoever created them.

    An organisation selection can point at a private binding, and the foreign key forbids
    deleting a binding while one does. So selections are dealt with first:

    * this organisation's ``chat`` selection is deleted (chat falls back to the platform
      default, which is what having no selection means);
    * this organisation's embedding selection stops the deactivation: dropping it could
      leave the index with vectors of a different size, so an administrator has to pick
      another binding first;
    * another organisation's selection is kept, and so is the binding row, emptied of its
      key and archived. That organisation then gets a plain "model not configured"
      answer and must choose again, and its audit log says why. One organisation's
      setup cannot block deactivating a member in another.
    """

    binding_ids = list(
        (
            await session.execute(
                select(AiProviderBinding.id)
                .where(AiProviderBinding.identity_id == user.identity_id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if not binding_ids:
        return PrivateModelClearResult(0, 0, 0)

    selections = (
        (
            await session.execute(
                select(AiModelSelection)
                .where(AiModelSelection.provider_binding_id.in_(binding_ids))
                .order_by(AiModelSelection.created_at, AiModelSelection.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    own = [s for s in selections if s.organization_id == user.organization_id]
    elsewhere = [s for s in selections if s.organization_id != user.organization_id]
    if any(s.purpose != "chat" for s in own):
        raise OffboardingRequestError(
            "org_embedding_uses_private_key",
            "组织的嵌入模型使用了此人的私有密钥,请先在 AI 设置中改用组织或平台模型,再停用此成员。 "
            "The organisation's embedding model uses this member's private key. "
            "Choose another model in the AI settings, then deactivate the member.",
        )

    own_chat_ids = [s.id for s in own]
    if own_chat_ids:
        await session.execute(
            delete(AiModelSelection).where(AiModelSelection.id.in_(own_chat_ids))
        )

    emptied = {s.provider_binding_id for s in elsewhere}
    for selection in elsewhere:
        session.add(
            AuditEvent(
                organization_id=selection.organization_id,
                actor_type="system",
                actor_id=None,
                action="ai_model_selection.invalidated",
                resource_type="ai_model_selection",
                resource_id=selection.id,
                payload={
                    "purpose": selection.purpose,
                    "reason": "private_binding_removed",
                },
            )
        )
    if emptied:
        await session.execute(
            update(AiProviderBinding)
            .where(AiProviderBinding.id.in_(emptied))
            .values(api_key_encrypted=None, status="archived")
        )
    gone = [b for b in binding_ids if b not in emptied]
    if gone:
        await session.execute(delete(AiProviderBinding).where(AiProviderBinding.id.in_(gone)))
    return PrivateModelClearResult(
        bindings_removed=len(binding_ids),
        org_selections_removed=len(own_chat_ids),
        other_org_selections_invalidated=len(elsewhere),
    )
