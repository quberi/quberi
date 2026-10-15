import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { AGENT_ICON_KEYS, AgentIcon } from './icons'

describe('AgentIcon', () => {
  it('renders an svg for every known icon key without throwing', () => {
    for (const key of AGENT_ICON_KEYS) {
      const { container, unmount } = render(<AgentIcon icon={key} />)
      expect(container.querySelector('svg')).not.toBeNull()
      unmount()
    }
  })

  it('falls back to the generic bot glyph for an unknown icon key', () => {
    const known = render(<AgentIcon icon="bot" />)
    const unknown = render(<AgentIcon icon="this-key-does-not-exist" />)
    // Both should render *some* svg (no crash), and the fallback's markup
    // should match the explicit "bot" icon's markup exactly.
    expect(unknown.container.querySelector('svg')).not.toBeNull()
    expect(unknown.container.innerHTML).toBe(known.container.innerHTML)
  })

  it('forwards size and strokeWidth to the rendered svg', () => {
    const { container } = render(<AgentIcon icon="bot" size={32} strokeWidth={2.5} />)
    const svg = container.querySelector('svg')
    expect(svg).toHaveAttribute('width', '32')
    expect(svg).toHaveAttribute('height', '32')
    expect(svg).toHaveAttribute('stroke-width', '2.5')
  })
})
