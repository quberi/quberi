import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'

import { useAuth } from '../auth/AuthContext'
import { useGroups } from '../groups/GroupsContext'
import { useMembers } from '../members/MembersContext'
import { useOrgUnits } from '../orgUnits/OrgUnitsContext'
import { usePositions } from '../positions/PositionsContext'

import type { AccessSubjectType } from './types'

export interface SubjectCandidate {
  id: string
  label: string
  subtitle: string
}

/** Placeholder id for the "whole organisation" candidate. It is never sent to
 * the API (see :func:`subjectIdForApi`) — the server substitutes the caller's
 * own organisation id. */
export const ORG_SUBJECT_ID = 'organization'

/** The ``subject_id`` to put on a request: omitted for the whole-organisation
 * subject, the picked id for everything else. */
export function subjectIdForApi(
  type: AccessSubjectType,
  id: string,
): string | undefined {
  return type === 'organization' ? undefined : id
}

/**
 * Who can be granted access to a space, per subject type. Backed by the
 * org-structure contexts the app already loads (members, units, positions,
 * groups), so every picker in the UI offers the same list.
 */
export function useSubjectCandidates(
  subjectType: AccessSubjectType,
): SubjectCandidate[] {
  const { t } = useTranslation()
  const { credentials } = useAuth()
  const { state: members } = useMembers()
  const { units } = useOrgUnits()
  const { positions } = usePositions()
  const { groups } = useGroups()

  return useMemo<SubjectCandidate[]>(() => {
    switch (subjectType) {
      case 'user':
        return members.kind === 'ready'
          ? members.items.map((m) => ({
              id: m.id,
              label: m.display_name,
              subtitle: m.email,
            }))
          : []
      case 'organization_unit':
        return units.kind === 'ready'
          ? units.items
              .filter((u) => u.status !== 'archived')
              .map((u) => ({ id: u.id, label: u.name, subtitle: u.type }))
          : []
      case 'position':
        return positions.kind === 'ready'
          ? positions.items
              .filter((p) => p.status !== 'archived')
              .map((p) => ({
                id: p.id,
                label: p.name,
                subtitle: p.grade ?? p.code ?? '',
              }))
          : []
      case 'group':
        return groups.kind === 'ready'
          ? groups.items
              .filter((g) => g.status !== 'archived')
              .map((g) => ({ id: g.id, label: g.name, subtitle: g.type }))
          : []
      case 'organization':
        return [
          {
            id: ORG_SUBJECT_ID,
            label:
              credentials?.organizationName ||
              credentials?.organizationSlug ||
              t('spaceAccess.wholeOrganization'),
            subtitle: t('spaceAccess.wholeOrganizationHint'),
          },
        ]
      default:
        return []
    }
  }, [subjectType, members, units, positions, groups, credentials, t])
}
