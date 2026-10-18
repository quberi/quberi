import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { UploadDropZone } from './UploadDropZone'

const docs = vi.hoisted(() => ({ upload: vi.fn(), uploads: [] as unknown[] }))

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (k: string, o?: Record<string, string>) => (o?.name ? `${k}|${o.name}|${o.modern}` : k),
  }),
}))
vi.mock('./DocumentsContext', () => ({ useDocuments: () => docs }))

function pick(container: HTMLElement, ...files: File[]) {
  const input = container.querySelector('input[type="file"]') as HTMLInputElement
  fireEvent.change(input, { target: { files } })
}

describe('UploadDropZone', () => {
  beforeEach(() => vi.clearAllMocks())

  it('turns an older Office file away with what to do, and uploads the rest', () => {
    const { container } = render(<UploadDropZone />)
    pick(
      container,
      new File(['x'], '报价.xls'),
      new File(['x'], '报价.xlsx'),
      new File(['x'], '合同.doc'),
    )
    expect(docs.upload).toHaveBeenCalledTimes(1)
    expect((docs.upload.mock.calls[0][0] as File).name).toBe('报价.xlsx')
    const alerts = screen.getByRole('alert').textContent ?? ''
    expect(alerts).toContain('documents.upload.legacy|报价.xls|.xlsx')
    expect(alerts).toContain('documents.upload.legacy|合同.doc|.docx')
  })

  it('does not offer the older formats in the file picker', () => {
    const { container } = render(<UploadDropZone />)
    const accept = (container.querySelector('input[type="file"]') as HTMLInputElement).accept
    expect(accept).toContain('.xlsx')
    expect(accept.split(',')).not.toContain('.xls')
    expect(accept.split(',')).not.toContain('.doc')
    expect(accept.split(',')).not.toContain('.ppt')
  })
})
