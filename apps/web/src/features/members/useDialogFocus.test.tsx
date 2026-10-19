import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useRef } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { useDialogFocus } from './useDialogFocus'

function Harness({ onClose, canClose = true }: { onClose: () => void; canClose?: boolean }) {
  const ref = useRef<HTMLDivElement>(null)
  useDialogFocus(ref, onClose, canClose)
  return (
    <div ref={ref} role="dialog" tabIndex={-1}>
      <button>first</button>
      <button>last</button>
    </div>
  )
}

function Page({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <>
      <button>opener</button>
      {open && <Harness onClose={onClose} />}
    </>
  )
}

describe('useDialogFocus', () => {
  it('moves focus in, keeps Tab inside, and closes on Escape', async () => {
    const onClose = vi.fn()
    render(<Harness onClose={onClose} />)
    expect(screen.getByText('first')).toHaveFocus()
    await userEvent.tab()
    expect(screen.getByText('last')).toHaveFocus()
    await userEvent.tab()
    expect(screen.getByText('first')).toHaveFocus() // wrapped, did not leave the dialog
    await userEvent.tab({ shift: true })
    expect(screen.getByText('last')).toHaveFocus()
    await userEvent.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('ignores Escape while it may not close', async () => {
    const onClose = vi.fn()
    render(<Harness onClose={onClose} canClose={false} />)
    await userEvent.keyboard('{Escape}')
    expect(onClose).not.toHaveBeenCalled()
  })

  it('gives focus back to what had it before', async () => {
    const { rerender } = render(<Page open={false} onClose={vi.fn()} />)
    screen.getByText('opener').focus()
    rerender(<Page open onClose={vi.fn()} />)
    expect(screen.getByText('first')).toHaveFocus()
    rerender(<Page open={false} onClose={vi.fn()} />)
    expect(screen.getByText('opener')).toHaveFocus()
  })
})
