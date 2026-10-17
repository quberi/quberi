import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { SpaceDocumentAccessPanel } from './SpaceDocumentAccessPanel'

const api = vi.hoisted(() => ({ listManagedDocuments: vi.fn() }))
const auth = vi.hoisted(() => ({ credentials: { sessionToken: 't' } }))

vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: (k: string) => k }) }))
vi.mock('../auth/AuthContext', () => ({ useAuth: () => auth }))
vi.mock('./accessRulesApi', () => api)
vi.mock('./DocumentAccessTab', () => ({ DocumentAccessTab: () => <div>access-tab</div> }))
vi.mock('./DocumentRow', () => ({ formatBytes: (n: number) => `${n} B` }))

const doc = {
  id: 'd1', name: 'plan.md', mime_type: 'text/markdown', size_bytes: 531,
  updated_at: '2026-01-01T00:00:00Z', allow_rules: 1, deny_rules: 0,
}

describe('SpaceDocumentAccessPanel', () => {
  beforeEach(() => vi.clearAllMocks())

  it('opens the access editor on the page itself, not inside the panel', async () => {
    api.listManagedDocuments.mockResolvedValue({ items: [doc], total: 1, limit: 20, offset: 0 })
    const { container } = render(<SpaceDocumentAccessPanel spaceId="s1" />)
    fireEvent.click(await screen.findByText('docAccessAdmin.setAccess'))
    const drawer = await screen.findByText('access-tab')
    // A panel traps fixed-position children, so the drawer must live outside it.
    expect(container.contains(drawer)).toBe(false)
    expect(document.body.contains(drawer)).toBe(true)
  })

  it('shows names and how many rules each document has', async () => {
    api.listManagedDocuments.mockResolvedValue({ items: [doc], total: 1, limit: 20, offset: 0 })
    render(<SpaceDocumentAccessPanel spaceId="s1" />)
    expect(await screen.findByText('plan.md')).toBeTruthy()
    expect(screen.getByText('docAccessAdmin.allowCount')).toBeTruthy()
    await waitFor(() => expect(api.listManagedDocuments).toHaveBeenCalledWith(auth.credentials, 's1', { q: '', offset: 0, limit: 20 }))
  })
})
