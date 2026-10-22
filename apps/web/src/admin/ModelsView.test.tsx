import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { ModelCatalogEntry, PlatformBindingDetail } from './api'
import { ModelsView, shownModels } from './ModelsView'

const api = vi.hoisted(() => ({
  listPlatformBindings: vi.fn(),
  createPlatformBinding: vi.fn(),
  updatePlatformBinding: vi.fn(),
  deletePlatformBinding: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => {
  const real = await importOriginal<typeof import('./api')>()
  return { ...real, ...api }
})

const entry = (id: string, over: Partial<ModelCatalogEntry> = {}): ModelCatalogEntry => ({
  model_id: id,
  provider_kind: 'openai_compat',
  purpose: 'chat',
  display_name: id,
  vendor: null,
  vendor_display_name: '',
  context_window: 1000,
  notes: '',
  supports_vision: false,
  ...over,
})

const CATALOG = [
  entry('deepseek-chat'),
  entry('deepseek-flash', { supports_vision: true }),
  entry('gpt-4o', { supports_vision: true }),
  entry('gpt-4o-mini'),
  entry('bge-m3', { purpose: 'embedding', provider_kind: 'local_embedding' }),
]

const binding = (over: Partial<PlatformBindingDetail>): PlatformBindingDetail =>
  ({
    id: 'k1',
    display_name: 'Quberi shared DeepSeek',
    provider_kind: 'openai_compat',
    vendor: 'deepseek',
    vendor_display_name: 'DeepSeek',
    base_url: 'https://api.deepseek.com',
    priority: 100,
    status: 'active',
    api_key_present: true,
    models: ['deepseek-chat'],
    ...over,
  }) as PlatformBindingDetail

const BINDINGS = [
  binding({}),
  binding({
    id: 'k2',
    display_name: 'ChatGPT',
    vendor: 'openai',
    vendor_display_name: 'OpenAI',
    base_url: 'https://api.openai.com/v1',
    models: ['gpt-4o', 'gpt-4o-mini'],
  }),
  binding({
    id: 'k3',
    display_name: 'Quberi local embeddings',
    provider_kind: 'local_embedding',
    vendor: null,
    vendor_display_name: '',
    base_url: null,
    models: ['bge-m3'],
  }),
]

function open() {
  return render(<ModelsView modelCatalog={CATALOG} token="t" />)
}

describe('ModelsView tabs', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.listPlatformBindings.mockResolvedValue({ items: BINDINGS })
    api.updatePlatformBinding.mockResolvedValue({})
    vi.stubGlobal('alert', vi.fn())
  })

  it('opens on Text models, which is the page as it was', async () => {
    open()
    expect(await screen.findByText('deepseek-chat')).toBeTruthy()
    expect(screen.getByText('gpt-4o, gpt-4o-mini')).toBeTruthy()
    expect(screen.getByText('bge-m3')).toBeTruthy()
    expect(screen.getByRole('tab', { name: 'Text models' }).getAttribute('aria-selected')).toBe('true')
  })

  it('Vision models shows the same keys but only the models that read pictures', async () => {
    open()
    await screen.findByText('deepseek-chat')

    fireEvent.click(screen.getByRole('tab', { name: 'Vision models' }))

    // The OpenAI key keeps the model that reads pictures and drops the one that does not.
    expect(screen.getByText('gpt-4o')).toBeTruthy()
    // The DeepSeek key serves no picture model yet, so it shows a dash but is still there to edit.
    expect(screen.queryByText('deepseek-chat')).toBeNull()
    expect(screen.getByText('Quberi shared DeepSeek')).toBeTruthy()
    // A local embedding key cannot carry a vision model.
    expect(screen.queryByText('Quberi local embeddings')).toBeNull()
    expect(screen.queryByText('bge-m3')).toBeNull()
  })

  it('adding a picture model to a key keeps the models it already served', async () => {
    open()
    await screen.findByText('deepseek-chat')
    fireEvent.click(screen.getByRole('tab', { name: 'Vision models' }))
    const row = screen.getByText('Quberi shared DeepSeek').closest('tr')!
    fireEvent.click(within(row).getByTitle('Edit'))

    // Only picture models are offered here...
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Models that read pictures this key serves')).toBeTruthy()
    expect(within(dialog).queryByText('deepseek-chat')).toBeNull()
    fireEvent.click(within(dialog).getByLabelText(/deepseek-flash/))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }))

    // ...and the one it already served is not lost.
    await waitFor(() => expect(api.updatePlatformBinding).toHaveBeenCalled())
    const [, id, body] = api.updatePlatformBinding.mock.calls[0]
    expect(id).toBe('k1')
    expect([...body.models].sort()).toEqual(['deepseek-chat', 'deepseek-flash'])
  })

  it('the Text models editor still offers chat models, not picture-only choices', async () => {
    open()
    await screen.findByText('deepseek-chat')
    const row = screen.getByText('Quberi shared DeepSeek').closest('tr')!
    fireEvent.click(within(row).getByTitle('Edit'))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Chat models this key serves')).toBeTruthy()
  })
})

describe('shownModels', () => {
  const vision = new Set(['gpt-4o'])
  it('shows everything on the text tab', () => {
    expect(shownModels(['gpt-4o', 'gpt-4o-mini'], 'text', vision)).toBe('gpt-4o, gpt-4o-mini')
  })
  it('shows only picture models on the vision tab, or a dash', () => {
    expect(shownModels(['gpt-4o', 'gpt-4o-mini'], 'vision', vision)).toBe('gpt-4o')
    expect(shownModels(['deepseek-chat'], 'vision', vision)).toBe('—')
    expect(shownModels([], 'text', vision)).toBe('—')
  })
})
