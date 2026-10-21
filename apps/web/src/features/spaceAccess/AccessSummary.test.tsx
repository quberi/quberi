import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { DocumentAccessSummaryCard } from '../documents/DocumentAccessSummaryCard'
import { AccessSources } from './AccessSources'
import { SpaceAccessSummaryCard } from './SpaceAccessSummaryCard'
import type { AccessSource, DocumentAccessSummary, SpaceAccessSummary } from './types'

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    // Echo the key with its numbers, so a test can see which sentence was chosen.
    t: (k: string, o?: Record<string, unknown>) =>
      o ? `${k}${o.count !== undefined ? `#${o.count}` : ''}` : k,
  }),
}))

const src = (over: Partial<AccessSource>): AccessSource => ({
  subject_type: 'user',
  subject_id: Math.random().toString(),
  name: 'Ann',
  role_code: 'viewer',
  ...over,
})

describe('AccessSources', () => {
  it('groups by kind, names departments and groups, and caps individual people', () => {
    render(
      <AccessSources
        sources={[
          src({ subject_type: 'organization_unit', name: 'Sales', role_code: 'editor' }),
          src({ subject_type: 'group', name: 'Reviewers' }),
          ...['A', 'B', 'C', 'D', 'E', 'F'].map((n) => src({ name: `Person ${n}` })),
        ]}
      />,
    )
    expect(screen.getByText('Sales')).toBeTruthy()
    expect(screen.getByText('Reviewers')).toBeTruthy()
    expect(screen.getByText('Person A')).toBeTruthy()
    expect(screen.queryByText('Person E')).toBeNull()
    expect(screen.getByText('spaceAccess.summary.moreUsers#2')).toBeTruthy()
  })

  it('says "whole organisation" instead of the organisation name', () => {
    render(<AccessSources sources={[src({ subject_type: 'organization', name: 'Acme Ltd' })]} />)
    expect(screen.getByText('spaceAccess.summary.wholeOrg')).toBeTruthy()
    expect(screen.queryByText('Acme Ltd')).toBeNull()
  })

  it('says so when the space is open to nobody', () => {
    render(<AccessSources sources={[]} />)
    expect(screen.getByText('spaceAccess.summary.nobody')).toBeTruthy()
  })
})

const space = (over: Partial<SpaceAccessSummary> = {}): SpaceAccessSummary => ({
  space_id: 's',
  status: 'active',
  people: 12,
  by_role: { owner: 1, editor: 3, viewer: 8 },
  sources: [src({ subject_type: 'organization_unit', name: 'Sales' })],
  documents: 20,
  documents_with_exceptions: 0,
  allow_rules: 0,
  deny_rules: 0,
  ...over,
})

describe('SpaceAccessSummaryCard', () => {
  it('shows the head count, the roles that have anyone, and who it is open to', () => {
    render(<SpaceAccessSummaryCard state={{ kind: 'ready', summary: space() }} />)
    expect(screen.getByText('12')).toBeTruthy()
    expect(screen.getByText('members.role.editor 3')).toBeTruthy()
    expect(screen.queryByText(/members.role.admin/)).toBeNull()
    expect(screen.getByText('Sales')).toBeTruthy()
    expect(screen.getByText('spaceAccess.summary.noExceptions#20')).toBeTruthy()
  })

  it('counts the documents that carry exception rules', () => {
    render(
      <SpaceAccessSummaryCard
        state={{
          kind: 'ready',
          summary: space({ documents_with_exceptions: 3, allow_rules: 2, deny_rules: 4 }),
        }}
      />,
    )
    expect(screen.getByText('spaceAccess.summary.someExceptions#3')).toBeTruthy()
  })

  it('shows the error rather than a wrong number', () => {
    render(<SpaceAccessSummaryCard state={{ kind: 'error', message: 'no way' }} />)
    expect(screen.getByText('no way')).toBeTruthy()
    expect(screen.queryByTestId('space-access-summary')).toBeNull()
  })
})

const doc = (over: Partial<DocumentAccessSummary> = {}): DocumentAccessSummary => ({
  document_id: 'd',
  space_id: 's',
  review_state: 'approved',
  space_people: 12,
  readers: 12,
  shared_only: 0,
  hidden: 0,
  allow_rules: 0,
  deny_rules: 0,
  sources: [],
  ...over,
})

describe('DocumentAccessSummaryCard', () => {
  it('shows the total and only the parts that are not zero', () => {
    render(<DocumentAccessSummaryCard summary={doc({ readers: 11, hidden: 1, deny_rules: 1 })} />)
    expect(screen.getByText('11')).toBeTruthy()
    expect(screen.getByText('docAccess.summary.hidden#1')).toBeTruthy()
    expect(screen.queryByText(/docAccess.summary.sharedOnly/)).toBeNull()
    expect(screen.getByText('docAccess.summary.someExceptions')).toBeTruthy()
  })

  it('shows both the extra and the missing people when a document has both kinds of rule', () => {
    render(
      <DocumentAccessSummaryCard
        summary={doc({ readers: 12, shared_only: 2, hidden: 2, allow_rules: 2, deny_rules: 2 })}
      />,
    )
    expect(screen.getByText('docAccess.summary.sharedOnly#2')).toBeTruthy()
    expect(screen.getByText('docAccess.summary.hidden#2')).toBeTruthy()
  })

  it('says there are no exceptions when there are none', () => {
    render(<DocumentAccessSummaryCard summary={doc()} />)
    expect(screen.getByText('docAccess.summary.noExceptions')).toBeTruthy()
  })

  it('does not invent a head count for a document that is not approved yet', () => {
    render(<DocumentAccessSummaryCard summary={doc({ readers: null, review_state: 'pending' })} />)
    expect(screen.getByText('docAccess.summary.notApproved')).toBeTruthy()
    expect(screen.queryByText('docAccess.summary.readers')).toBeNull()
  })
})
