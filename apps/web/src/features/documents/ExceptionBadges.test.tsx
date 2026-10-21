import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { ExceptionBadges } from './ExceptionBadges'

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (k: string, o?: { count?: number }) => (o?.count !== undefined ? `${k}#${o.count}` : k),
  }),
}))

describe('ExceptionBadges', () => {
  it('shows nothing for a document without rules, or when the server sent no counts', () => {
    const { container, rerender } = render(<ExceptionBadges allow={0} deny={0} />)
    expect(container.textContent).toBe('')
    rerender(<ExceptionBadges />)
    expect(container.textContent).toBe('')
    rerender(<ExceptionBadges allow={null} deny={null} />)
    expect(container.textContent).toBe('')
  })

  it('marks a document that is shared outside the space', () => {
    render(<ExceptionBadges allow={2} deny={0} />)
    expect(screen.getByText('docAccessAdmin.allowCount#2')).toBeTruthy()
    expect(screen.queryByText(/denyCount/)).toBeNull()
  })

  it('marks a document that is hidden from some people', () => {
    render(<ExceptionBadges allow={0} deny={3} />)
    expect(screen.getByText('docAccessAdmin.denyCount#3')).toBeTruthy()
    expect(screen.queryByText(/allowCount/)).toBeNull()
  })

  it('marks both when a document has both kinds of rule', () => {
    render(<ExceptionBadges allow={1} deny={1} />)
    expect(screen.getByText('docAccessAdmin.allowCount#1')).toBeTruthy()
    expect(screen.getByText('docAccessAdmin.denyCount#1')).toBeTruthy()
  })
})
