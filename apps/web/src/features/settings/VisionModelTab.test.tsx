import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../api/client'
import type { ModelSelectionSlot, UsableChatModel } from './api'
import { VisionModelTab, groupByVendor } from './VisionModelTab'

const api = vi.hoisted(() => ({
  getOrganizationSettings: vi.fn(),
  updateOrganizationSettings: vi.fn(),
  setModelSelection: vi.fn(),
  clearModelSelection: vi.fn(),
}))

vi.mock('./api', () => api)
vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (k: string, o?: Record<string, unknown>) => (o ? `${k} ${JSON.stringify(o)}` : k),
  }),
}))
const auth = vi.hoisted(() => ({ credentials: { sessionToken: 't' } }))
vi.mock('../auth/AuthContext', () => ({ useAuth: () => auth }))
vi.mock('./modelDescriptions', () => ({ describeModel: (id: string) => `about ${id}` }))

const model = (id: string, over: Partial<UsableChatModel> = {}): UsableChatModel => ({
  model_id: id,
  display_name: id,
  binding_id: 'b1',
  binding_name: 'Platform key',
  is_platform_default: true,
  vendor: 'deepseek',
  is_default: false,
  is_private: false,
  supports_vision: false,
  ...over,
})

const USABLE = [
  model('deepseek-chat'),
  model('deepseek-flash', { supports_vision: true }),
  model('gpt-4o', { supports_vision: true, vendor: 'openai', binding_id: 'b2' }),
  model('mine', { binding_id: 'b3', binding_name: 'My key', vendor: null, is_private: true }),
]
const NONE: ModelSelectionSlot = { model_id: null, binding_id: null }

function renderTab(selection: ModelSelectionSlot = NONE, onChanged = vi.fn().mockResolvedValue(undefined)) {
  render(<VisionModelTab onChanged={onChanged} selection={selection} usable={USABLE} />)
  return onChanged
}

describe('VisionModelTab', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getOrganizationSettings.mockResolvedValue({ image_vision_enabled: false })
    api.setModelSelection.mockResolvedValue(undefined)
    api.clearModelSelection.mockResolvedValue(undefined)
    vi.stubGlobal('confirm', vi.fn().mockReturnValue(true))
  })

  it('lists only the models that read pictures, grouped by company, with a description', async () => {
    renderTab()
    expect(await screen.findByText('deepseek-flash')).toBeTruthy()
    expect(screen.getByText('gpt-4o')).toBeTruthy()
    expect(screen.queryByText('deepseek-chat')).toBeNull()
    expect(screen.getByText('about deepseek-flash')).toBeTruthy()
    expect(screen.getByText('DeepSeek')).toBeTruthy()
    expect(screen.getByText('OpenAI')).toBeTruthy()
  })

  it('says nothing is sent while no model is chosen, and does not let the switch be turned on', async () => {
    renderTab()
    expect(await screen.findByText('modelSettings.vision.none')).toBeTruthy()
    const toggle = screen.getByRole('checkbox') as HTMLInputElement
    expect(toggle.disabled).toBe(true)
    expect(screen.getByText('modelSettings.vision.chooseFirst')).toBeTruthy()
  })

  it('chooses a model for the vision purpose and then reloads', async () => {
    const onChanged = renderTab()
    fireEvent.click((await screen.findAllByRole('radio'))[0])
    await waitFor(() =>
      expect(api.setModelSelection).toHaveBeenCalledWith(auth.credentials, 'vision', 'deepseek-flash', 'b1'),
    )
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
  })

  it('asks before turning descriptions on, naming where the pictures go', async () => {
    api.updateOrganizationSettings.mockResolvedValue({ image_vision_enabled: true })
    renderTab({ model_id: 'deepseek-flash', binding_id: 'b1' })
    const toggle = (await screen.findByRole('checkbox')) as HTMLInputElement
    await waitFor(() => expect(toggle.disabled).toBe(false))

    fireEvent.click(toggle)

    await waitFor(() =>
      expect(api.updateOrganizationSettings).toHaveBeenCalledWith(auth.credentials, {
        image_vision_enabled: true,
      }),
    )
    expect(window.confirm).toHaveBeenCalledWith(
      'modelSettings.vision.confirm {"provider":"DeepSeek"}',
    )
  })

  it('sends nothing and changes nothing when the owner declines', async () => {
    vi.stubGlobal('confirm', vi.fn().mockReturnValue(false))
    renderTab({ model_id: 'deepseek-flash', binding_id: 'b1' })
    const toggle = (await screen.findByRole('checkbox')) as HTMLInputElement
    await waitFor(() => expect(toggle.disabled).toBe(false))

    fireEvent.click(toggle)

    expect(api.updateOrganizationSettings).not.toHaveBeenCalled()
  })

  it('turning it off needs no confirmation', async () => {
    api.getOrganizationSettings.mockResolvedValue({ image_vision_enabled: true })
    api.updateOrganizationSettings.mockResolvedValue({ image_vision_enabled: false })
    renderTab({ model_id: 'deepseek-flash', binding_id: 'b1' })
    const toggle = (await screen.findByRole('checkbox')) as HTMLInputElement
    await waitFor(() => expect(toggle.checked).toBe(true))

    fireEvent.click(toggle)

    await waitFor(() =>
      expect(api.updateOrganizationSettings).toHaveBeenCalledWith(auth.credentials, {
        image_vision_enabled: false,
      }),
    )
    expect(window.confirm).not.toHaveBeenCalled()
  })

  it('shows the reason when the server refuses', async () => {
    api.setModelSelection.mockRejectedValue(new ApiError(403, 'owners and admins only'))
    renderTab()
    fireEvent.click((await screen.findAllByRole('radio'))[0])
    expect((await screen.findByText('owners and admins only'))).toBeTruthy()
  })

  it('accepts a model name typed for the administrator\'s own endpoint, on a key they pick', async () => {
    renderTab()
    const use = await screen.findByText('modelSettings.vision.customUse')
    expect((use as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(screen.getByDisplayValue('modelSettings.vision.customBindingPlaceholder'), {
      target: { value: 'b1' },
    })
    fireEvent.change(screen.getByPlaceholderText('qwen2.5-vl-72b-instruct'), {
      target: { value: '  my-vl-72b ' },
    })

    fireEvent.click(use)

    await waitFor(() =>
      expect(api.setModelSelection).toHaveBeenCalledWith(auth.credentials, 'vision', 'my-vl-72b', 'b1'),
    )
  })

  it('never offers a members private key for the organisation to use', async () => {
    renderTab()
    await screen.findByText('modelSettings.vision.customUse')
    const options = screen.getAllByRole('option').map((o) => o.textContent)
    expect(options).toContain('Platform key')
    expect(options).not.toContain('My key')
  })

  it('can stop using a vision model', async () => {
    const onChanged = renderTab({ model_id: 'deepseek-flash', binding_id: 'b1' })
    fireEvent.click(await screen.findByText('modelSettings.vision.clear'))
    await waitFor(() =>
      expect(api.clearModelSelection).toHaveBeenCalledWith(auth.credentials, 'vision'),
    )
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
  })
})

describe('groupByVendor', () => {
  it('uses the company name, and the key name for a model with no vendor', () => {
    const groups = groupByVendor([
      model('a', { vendor: 'moonshot' }),
      model('b', { vendor: null, binding_name: 'Our endpoint' }),
      model('c', { vendor: 'moonshot' }),
    ])
    expect(groups.map(([name, list]) => [name, list.length])).toEqual([
      ['Kimi · Moonshot', 2],
      ['Our endpoint', 1],
    ])
  })
})
