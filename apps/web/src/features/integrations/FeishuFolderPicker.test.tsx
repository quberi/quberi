import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { FeishuFolderPicker } from './FeishuFolderPicker'

const browse = vi.fn()
// Must be a stable object, like the real auth context's, or the component
// would see "new credentials" on every render and reload forever.
const credentials = { token: 'stable-credentials' }

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}))
vi.mock('../auth/AuthContext', () => ({
  useAuth: () => ({ credentials }),
}))
vi.mock('./api', () => ({
  feishuBrowseFolders: (...args: unknown[]) => browse(...args),
}))

describe('FeishuFolderPicker', () => {
  beforeEach(() => {
    browse.mockReset()
    browse.mockResolvedValue({
      items: [{ token: 'fld1', name: 'test', type: 'folder', has_children: true }],
      needs_auth: false,
    })
  })

  it('does not reload the folder tree when the parent re-renders with new callbacks', async () => {
    const { rerender } = render(
      <FeishuFolderPicker
        onNeedsAuth={() => {}}
        onSelect={() => {}}
        selectedToken={null}
      />,
    )
    await waitFor(() => expect(screen.getByText('test')).toBeInTheDocument())
    expect(browse).toHaveBeenCalledTimes(1)

    // The parent form re-renders on every keystroke and hands down fresh
    // function objects each time; that must not trigger another request.
    for (let i = 0; i < 5; i += 1) {
      rerender(
        <FeishuFolderPicker
          onNeedsAuth={() => {}}
          onSelect={() => {}}
          selectedToken={null}
        />,
      )
    }
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(browse).toHaveBeenCalledTimes(1)
    expect(screen.getByText('test')).toBeInTheDocument()
  })

  it('tells the parent when Feishu authorisation is missing, using the latest callback', async () => {
    browse.mockResolvedValue({ items: [], needs_auth: true })
    const first = vi.fn()
    const latest = vi.fn()
    const { rerender } = render(
      <FeishuFolderPicker onNeedsAuth={first} onSelect={() => {}} selectedToken={null} />,
    )
    rerender(
      <FeishuFolderPicker onNeedsAuth={latest} onSelect={() => {}} selectedToken={null} />,
    )
    await waitFor(() => expect(latest.mock.calls.length + first.mock.calls.length).toBe(1))
    // The call goes to whichever callback is current when the answer arrives.
    expect(latest).toHaveBeenCalledTimes(1)
    expect(first).not.toHaveBeenCalled()
  })
})
