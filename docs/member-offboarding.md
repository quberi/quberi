# Member offboarding

When someone leaves, the organization owner can deactivate their account.
This page describes what deactivation does, what it does not do, and how to
hand off what the member leaves behind.

Deactivation and reactivation only exist as API endpoints today; there is no
dedicated screen yet. The organization's **members** page shows the member's
status and a checklist of anything that still needs a new owner.

## Who can do this

Only the **organization owner** can deactivate, reactivate, or view a
member's offboarding checklist. Organization admins cannot. The owner cannot
deactivate themselves, and the owner's own account can never be deactivated —
there is always someone who can undo a deactivation.

## What deactivating a member does

Deactivation is one atomic action. All of the following happen together, or
none of them do:

- The member's account is marked unavailable. Every sign-in method (email
  code, phone, Google, Feishu) refuses it from that point on.
- Every web session the member is currently signed into stops working
  immediately — there is no waiting for a session to expire.
- Every outside-tool connection (OAuth grant) the member authorized is
  revoked at once. A connected plugin using their access will start getting
  "account unavailable" the next time it calls in.
- Any Feishu sync the member set up personally is paused, and their personal
  Feishu credential is removed.
- Any private model key (bring-your-own-key) the member added for themselves
  is removed. If the organization's chat or embedding model was using one of
  those keys, the organization automatically falls back to its own or the
  platform default model; you will see this in the deactivation confirmation
  before you proceed.
- The member's **personal space** is frozen (see below).

Content the member uploaded into **shared** spaces is not touched. Other
members with access to those spaces can keep searching it and reading it,
same as before.

## Personal space freezing

Every member has a personal space for their own notes and uploads. It is
private by design — not even the organization owner or an admin can read it,
search it, or see what is in it.

When a member is deactivated, their personal space is frozen:

- Nobody — including the organization owner — can open it, list what's in
  it, search it, or see citations that quote from it in old chats.
- Nothing is deleted. The content is preserved exactly as it was.
- If the member is later reactivated, their personal space unfreezes
  automatically and everything is exactly as they left it.

This is intentionally the only thing that happens to a personal space.
Quberi does not offer a way for the organization to inspect, export, or
reassign a departed member's personal space. If your organization has a
legal or compliance reason to access that content, that is outside this
product today.

## Offboarding checklist and handing off spaces

A deactivated member may still be the owner of shared (non-personal) spaces,
or the designated reviewer for uploads awaiting approval. Nobody else can act
on those until they are handed to someone else.

The owner can open the member's **offboarding checklist**, which lists:

- Spaces (active or archived, not personal) the member owns.
- Uploads awaiting the member's review.

From there, the owner can **batch-transfer** the listed spaces to another
active member in one action. The chosen person must be an active member,
cannot be the organization owner, and cannot be the member being offboarded.
Each space is transferred independently, so a batch with 20 spaces and one
problem space still transfers the other 19 — the result tells you which
succeeded, which were already handled by someone else, and which failed and
why. Running the same transfer again is safe: anything already transferred
is simply reported as already done.

## Reactivating a member

Reactivation reverses exactly what deactivation switched off: the account
can sign in again, and its personal space unfreezes. It does not restore
anything that was actively removed — OAuth connections, the Feishu personal
credential, and private model keys stay gone and must be set up again.

## Scope and limits

- **Agents are out of scope for now.** A "fixed scope" agent that was
  explicitly configured to search a specific set of spaces does not check
  whether those spaces are frozen. Freezing a personal space does not fully
  close this off if a fixed-scope agent already pointed at it. This will be
  addressed when agents are redesigned.
- **Membership, not identity.** If the same person belongs to more than one
  organization, deactivating them in one organization has no effect on their
  membership or sign-in in any other organization. Each organization's
  membership is independent.
- **Archived (not personal) spaces the member owned** are not automatically
  handed off by deactivation; they appear in the checklist above so the
  owner can transfer them explicitly.
- Deactivation does not delete the member's account, their messages in
  shared conversations, or anything they contributed outside their personal
  space.
