import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  AdminApiError,
  type ModelCatalogEntry,
  type OAuthClientList,
  type OrganizationStatsRow,
  type PlatformBindingDetail,
  type PlatformOverview,
  type TrafficSummary,
} from './api'
import { ClientsView } from './ClientsView'
import { LANG_STORAGE_KEY, setLang } from './i18n'
import { ModelsView } from './ModelsView'
import { OrganizationsView } from './OrganizationsView'
import { OverviewView } from './OverviewView'
import { TrafficView } from './TrafficView'

const api = vi.hoisted(() => ({
  getTraffic: vi.fn(),
  listPlatformBindings: vi.fn(),
  deletePlatformBinding: vi.fn(),
  listOAuthClients: vi.fn(),
  deleteOAuthClient: vi.fn(),
  getOrgPlatformModelVisibility: vi.fn(),
  setOrgPlatformModelVisibility: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => {
  const real = await importOriginal<typeof import('./api')>()
  return { ...real, ...api }
})

beforeEach(() => {
  Object.values(api).forEach((f) => f.mockReset())
  window.localStorage.clear()
  setLang('zh')
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  window.localStorage.removeItem(LANG_STORAGE_KEY)
  setLang('en')
  window.localStorage.removeItem(LANG_STORAGE_KEY)
})

/** Text that would be English prose: three or more letters in a row outside <code>. */
function visibleProse(root: HTMLElement): string[] {
  const found: string[] = []
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node.textContent ?? ''
    if (node.parentElement?.closest('code')) continue
    if (/[A-Za-z]{3,}/.test(text)) found.push(text)
  }
  return found
}

describe('OverviewView (zh)', () => {
  const overview: PlatformOverview = {
    totals: {
      organizations: 1234,
      organizations_personal: 1000,
      organizations_team: 234,
      users: 5,
      identities: 4321,
      spaces: 3,
      documents: 2000,
      documents_indexed: 1500,
      conversations: 9,
      messages: 12000,
    },
    new_organizations_24h: 2,
    new_organizations_7d: 9,
    new_identities_24h: 3,
    new_identities_7d: 11,
    active_organizations_7d: 4,
    signups_by_day: [{ day: '2026-10-22', organizations: 1, identities: 2 }],
  }

  it('shows Chinese labels, sub lines and the chart text', () => {
    const { container } = render(<OverviewView overview={overview} />)
    expect(screen.getByRole('heading', { name: '概览' })).toBeTruthy()
    expect(screen.getByText('活跃组织（7 天）')).toBeTruthy()
    expect(screen.getByText('个人 1000 个 · 团队 234 个')).toBeTruthy()
    expect(screen.getByText('24 小时新增 3 人 · 7 天新增 11 人')).toBeTruthy()
    expect(screen.getByText('至少发送过一条消息')).toBeTruthy()
    expect(screen.getByText('已索引 1500 篇')).toBeTruthy()
    expect(screen.getByRole('img', { name: '每日注册数，近 14 天' })).toBeTruthy()
    expect(visibleProse(container)).toEqual([])
  })

  it('formats numbers for the active locale', () => {
    render(<OverviewView overview={overview} />)
    expect(screen.getAllByText('12,000').length).toBeGreaterThan(0)
  })
})

describe('OrganizationsView (zh)', () => {
  const org = (over: Partial<OrganizationStatsRow> = {}): OrganizationStatsRow => ({
    id: 'o1',
    name: 'Example Org',
    slug: 'example-org',
    tier: 'team',
    status: 'active',
    created_at: '2026-10-21T08:00:00Z',
    member_count: 3,
    space_count: 2,
    document_count: 1,
    conversation_count: 4,
    message_count: 5,
    owner_email: 'owner@example.test',
    is_demo: true,
    ...over,
  })
  const catalog: ModelCatalogEntry[] = []

  it('translates headers, tier, demo chip and the empty row', () => {
    const { container, rerender } = render(
      <OrganizationsView orgs={[org(), org({ id: 'o2', tier: 'personal', is_demo: false })]} token="t" modelCatalog={catalog} />,
    )
    expect(screen.getByRole('heading', { name: /组织（2）/ })).toBeTruthy()
    for (const header of ['名称', '套餐', '所有者', '成员', '空间', '文档', '对话', '消息', '创建时间']) {
      expect(screen.getByRole('columnheader', { name: header })).toBeTruthy()
    }
    expect(screen.getByText('团队')).toBeTruthy()
    expect(screen.getByText('个人')).toBeTruthy()
    expect(screen.getByText('演示')).toBeTruthy()
    expect(visibleProse(container).filter((s) => !/example|owner/i.test(s))).toEqual([])

    rerender(<OrganizationsView orgs={[]} token="t" modelCatalog={catalog} />)
    expect(screen.getByText('暂无组织。')).toBeTruthy()
  })

  it('translates the drawer and its errors', async () => {
    api.getOrgPlatformModelVisibility.mockResolvedValue({ mode: 'allowlist', model_ids: [] })
    render(<OrganizationsView orgs={[org()]} token="t" modelCatalog={catalog} />)
    fireEvent.click(screen.getByText('Example Org'))
    const dialog = await screen.findByRole('dialog')
    expect(await within(dialog).findByText('平台共享模型可见性')).toBeTruthy()
    expect(within(dialog).getByText('允许列表')).toBeTruthy()
    expect(within(dialog).getByText('只有下方勾选的模型可见。')).toBeTruthy()
    expect(within(dialog).getByText('模型注册表中没有对话模型。')).toBeTruthy()
    expect(within(dialog).getByRole('button', { name: '关闭' })).toBeTruthy()
  })

  it('shows a load error from its code in Chinese', async () => {
    api.getOrgPlatformModelVisibility.mockRejectedValue(
      new AdminApiError(404, 'Organization not found: x', 'admin.orgs.not_found', { id: 'x' }),
    )
    render(<OrganizationsView orgs={[org()]} token="t" modelCatalog={catalog} />)
    fireEvent.click(screen.getByText('Example Org'))
    expect(await screen.findByText('找不到组织：x')).toBeTruthy()
  })
})

describe('ModelsView (zh)', () => {
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
  const catalog = [entry('deepseek-chat'), entry('deepseek-flash', { supports_vision: true })]
  const binding = (over: Partial<PlatformBindingDetail> = {}): PlatformBindingDetail =>
    ({
      id: 'k1',
      display_name: 'Shared DeepSeek',
      provider_kind: 'openai_compat',
      vendor: 'deepseek',
      vendor_display_name: 'DeepSeek',
      base_url: 'https://api.example.test/v1',
      priority: 100,
      status: 'paused',
      api_key_present: true,
      models: ['deepseek-chat'],
      ...over,
    }) as PlatformBindingDetail

  it('translates tabs, headers, status chip and the vision hint with its code element', async () => {
    api.listPlatformBindings.mockResolvedValue({ items: [binding()] })
    const { container } = render(<ModelsView token="t" modelCatalog={catalog} />)
    expect(await screen.findByText('Shared DeepSeek')).toBeTruthy()
    expect(screen.getByRole('tab', { name: '文本模型' })).toBeTruthy()
    for (const header of ['名称', '基础 URL', '优先级', '状态', '模型']) {
      expect(screen.getByRole('columnheader', { name: header })).toBeTruthy()
    }
    const chip = screen.getByText('已暂停')
    expect(chip.className).toContain('admin-chip-status-paused')
    expect(screen.getByText('已配置密钥')).toBeTruthy()
    expect(screen.getByTitle('编辑')).toBeTruthy()
    expect(screen.getByTitle('删除')).toBeTruthy()

    fireEvent.click(screen.getByRole('tab', { name: '视觉模型' }))
    const code = container.querySelector('p.admin-section-hint code')
    expect(code?.textContent).toBe('deepseek-flash')
  })

  it('asks for delete confirmation in Chinese, with the key name', async () => {
    api.listPlatformBindings.mockResolvedValue({ items: [binding()] })
    api.deletePlatformBinding.mockResolvedValue(undefined)
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    render(<ModelsView token="t" modelCatalog={catalog} />)
    fireEvent.click(await screen.findByTitle('删除'))
    expect(confirm).toHaveBeenCalledWith('删除平台密钥“Shared DeepSeek”？此操作无法撤销。')
    expect(api.deletePlatformBinding).not.toHaveBeenCalled()
  })

  it('shows the editor in Chinese', async () => {
    api.listPlatformBindings.mockResolvedValue({ items: [binding()] })
    render(<ModelsView token="t" modelCatalog={catalog} />)
    fireEvent.click(await screen.findByText('添加密钥'))
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText('添加平台密钥')).toBeTruthy()
    expect(within(dialog).getByText('供应商')).toBeTruthy()
    expect(within(dialog).getByPlaceholderText('例如 deepseek、openai、dashscope')).toBeTruthy()
    expect(within(dialog).getByText('此密钥提供的对话模型')).toBeTruthy()
    expect(within(dialog).getByText('取消')).toBeTruthy()
    expect(within(dialog).getByText('保存')).toBeTruthy()
  })

  it('renders a coded load error from the dictionary', async () => {
    api.listPlatformBindings.mockRejectedValue(
      new AdminApiError(404, 'Platform key not found.', 'admin.models.binding_not_found'),
    )
    render(<ModelsView token="t" modelCatalog={catalog} />)
    expect(await screen.findByText('找不到该平台密钥。')).toBeTruthy()
  })
})

describe('ClientsView (zh)', () => {
  const list = (over: Partial<OAuthClientList> = {}): OAuthClientList => ({
    items: [
      {
        id: 'r1',
        client_id: 'quberi-mcp',
        kind: 'registered',
        name: 'MCP client',
        redirect_uris: ['loopback'],
        enabled: true,
        allow_mcp: true,
        note: null,
        created_at: '2026-10-21T00:00:00Z',
        updated_at: '2026-10-21T00:00:00Z',
      },
    ],
    rejected: [
      {
        client_id: 'https://tool.example.test/oauth/abc123/client.json',
        redirect_uri: 'https://tool.example.test/callback/abc123',
        reason: 'unknown_client',
        at: '2026-10-22T09:30:00Z',
      },
      {
        client_id: 'quberi-mcp',
        redirect_uri: 'https://evil.example.test/cb',
        reason: 'redirect_not_allowed',
        at: '2026-10-22T09:31:00Z',
      },
      {
        client_id: 'x',
        redirect_uri: 'https://x.example.test/cb',
        reason: 'brand_new_reason',
        at: '2026-10-22T09:32:00Z',
      },
    ],
    advertises_metadata_documents: false,
    ...over,
  })

  it('translates the page, the hint (with code elements) and the refused list', async () => {
    api.listOAuthClients.mockResolvedValue(list())
    const { container } = render(<ClientsView token="t" />)
    expect(await screen.findByText('MCP client')).toBeTruthy()
    expect(screen.getByRole('heading', { name: /外部工具/ })).toBeTruthy()
    const codes = [...container.querySelectorAll('p.admin-section-hint code')].map((c) => c.textContent)
    expect(codes).toEqual(['loopback', 'https://', '{id}'])
    expect(screen.getByTestId('advertising').textContent).toContain('未公布')
    expect(screen.getByText('已注册 ID · MCP 服务器')).toBeTruthy()
    expect(screen.getByText('已启用')).toBeTruthy()
    expect(screen.getByTitle('点击停用')).toBeTruthy()
    expect(screen.getByText('最近被拒绝')).toBeTruthy()
    expect(screen.getByText('未知工具')).toBeTruthy()
    expect(screen.getByText('回调地址不被允许')).toBeTruthy()
    // An unknown reason falls back to the raw value.
    expect(screen.getByText('brand_new_reason')).toBeTruthy()
    expect(screen.getAllByRole('button', { name: '授权…' })).toHaveLength(1)
  })

  it('says so when metadata documents are advertised', async () => {
    api.listOAuthClients.mockResolvedValue(list({ advertises_metadata_documents: true }))
    render(<ClientsView token="t" />)
    const line = await screen.findByTestId('advertising')
    expect(line.textContent).toContain('已公布')
    expect(line.textContent).not.toContain('未公布')
  })

  it('shows the empty states', async () => {
    api.listOAuthClients.mockResolvedValue(list({ items: [], rejected: [] }))
    render(<ClientsView token="t" />)
    expect(await screen.findByText('尚未授权任何工具，因此无人能连接外部工具。')).toBeTruthy()
    expect(screen.getByText('最近没有被拒绝的记录。')).toBeTruthy()
  })

  it('confirms removal in Chinese, with the tool name', async () => {
    api.listOAuthClients.mockResolvedValue(list())
    api.deleteOAuthClient.mockResolvedValue(undefined)
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    render(<ClientsView token="t" />)
    fireEvent.click(await screen.findByTitle('移除'))
    expect(confirm).toHaveBeenCalledWith(
      '移除“MCP client”？用户将无法再连接它，其现有连接也会停止刷新。',
    )
    expect(api.deleteOAuthClient).not.toHaveBeenCalled()
  })

  it('shows the editor in Chinese', async () => {
    api.listOAuthClients.mockResolvedValue(list())
    render(<ClientsView token="t" />)
    fireEvent.click(await screen.findByText('添加工具'))
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText('授权外部工具')).toBeTruthy()
    expect(within(dialog).getByText('向用户显示的名称')).toBeTruthy()
    expect(within(dialog).getByText('工具如何标识自己')).toBeTruthy()
    expect(within(dialog).getByText('授权码可发送到（每行一个）')).toBeTruthy()
    expect(within(dialog).getByText('已启用')).toBeTruthy()
    expect(within(dialog).getByText('可使用 MCP 服务器')).toBeTruthy()
    expect(within(dialog).getByText('备注（可选）')).toBeTruthy()
  })

  it('shows a coded error with its parameters in Chinese', async () => {
    api.listOAuthClients.mockRejectedValue(
      new AdminApiError(400, 'x', 'admin.oauth.url_not_https', {
        subject: { code: 'admin.oauth.subject.redirect', params: { rule: "'http://a.example.test'" } },
      }),
    )
    render(<ClientsView token="t" />)
    expect(
      await screen.findByText("回调地址 'http://a.example.test'必须是 https:// 地址。"),
    ).toBeTruthy()
  })
})

describe('TrafficView (zh)', () => {
  const base = (over: Partial<TrafficSummary> = {}): TrafficSummary => ({
    enabled: true,
    reason: null,
    window: { from: '2026-10-22T08:10:00Z', to: '2026-10-23T08:10:00Z' },
    files_read: 1,
    truncated_by_cap: true,
    address_note: 'Server note in whatever language.',
    address_note_code: 'admin.traffic.address_note',
    totals: { requests: 2000, distinct_visitors: 31, automated_requests: 1500, automated_share: 0.75 },
    by_site: [
      { host: 'app.example.test', requests: 120 },
      { host: '(unknown host)', requests: 50 },
      { host: '(other host)', requests: 5 },
    ],
    by_kind: [
      { kind: 'search_engine', requests: 20 },
      { kind: 'mcp_client', requests: 10 },
    ],
    by_status_class: [
      { status_class: '2xx', requests: 90 },
      { status_class: 'other', requests: 1 },
    ],
    by_country: [{ country: 'unknown', requests: 60 }],
    mcp: {
      top_user_agents: [{ user_agent: '', kind: 'scanner_or_tool', requests: 25 }],
      top_paths: [],
    },
    web_pages: [],
    signals: { guest_questions: 4, sign_in_requests: 2, oauth_requests: 1, mcp_successful_posts: 6 },
    top_visitors: [
      {
        address: 'unknown',
        address_source: 'header',
        kind: 'browser_like',
        country: 'unknown',
        user_agent: '',
        requests: 14,
      },
      {
        address: '203.0.113.x',
        address_source: 'remote_ip',
        kind: 'scanner_or_tool',
        country: 'US',
        user_agent: 'curl/8.7.1',
        requests: 9,
      },
    ],
    hourly: [
      { hour: '2026-10-23T07:00:00Z', requests: 20, automated_requests: 15 },
      { hour: '2026-10-23T08:00:00Z', requests: 180, automated_requests: 135 },
    ],
    ...over,
  })
  const disabled = (reason: TrafficSummary['reason']) =>
    base({ enabled: false, reason, by_site: [], by_country: [], hourly: [] })

  it('translates cards, blocks, kinds and sentinels', async () => {
    api.getTraffic.mockResolvedValue(base())
    const { container } = render(<TrafficView token="t" onSignOut={() => {}} />)
    expect(await screen.findByText('独立访客')).toBeTruthy()
    expect(screen.getByText('2026-10-22 08:10 至 2026-10-23 08:10 UTC · 已读取 1 个日志文件')).toBeTruthy()
    expect(screen.getByText(/日志超过了读取上限/)).toBeTruthy()
    expect(screen.getByText('2,000', { selector: '.admin-kpi-value' })).toBeTruthy()
    expect(screen.getByText('1,500 个自动化请求')).toBeTruthy()
    expect(screen.getByText('真实使用迹象（已排除机器人）')).toBeTruthy()
    expect(screen.getByText('每小时请求数（UTC）')).toBeTruthy()
    expect(screen.getByRole('img', { name: '每小时请求数：共 2000 个，其中自动化 1500 个' })).toBeTruthy()

    const kinds = within(screen.getByRole('table', { name: '按访客类型统计的请求' }))
    expect(kinds.getByText('搜索引擎')).toBeTruthy()
    expect(kinds.getByText('连接 MCP 端点的工具（Codex、Claude、Cursor 等）。')).toBeTruthy()

    const sites = within(screen.getByRole('table', { name: '按站点统计的请求' }))
    expect(sites.getByText('（未知主机）')).toBeTruthy()
    expect(sites.getByText('（其他主机）')).toBeTruthy()
    expect(sites.getByText('app.example.test')).toBeTruthy()
    expect(
      within(screen.getByRole('table', { name: '按国家/地区统计的请求' })).getByText('未知'),
    ).toBeTruthy()
    expect(
      within(screen.getByRole('table', { name: '按响应类别统计的请求' })).getByText('其他'),
    ).toBeTruthy()

    const visitors = within(screen.getByRole('table', { name: '访问最多的访客' }))
    expect(visitors.getByTitle('取自 CDN 设置的请求头')).toBeTruthy()
    expect(visitors.getByTitle('连接地址')).toBeTruthy()
    expect(visitors.getAllByText('（无）').length).toBe(1)
    expect(within(screen.getByRole('table', { name: 'MCP User-Agent' })).getByText('（无）')).toBeTruthy()
    expect(within(screen.getByRole('table', { name: 'MCP 路径' })).getByText('此时间范围内没有请求。')).toBeTruthy()

    const prose = visibleProse(container).filter(
      (s) => !/example|curl|Codex|MCP|Quberi|UTC|OAuth|User-Agent|remote_ip|header/i.test(s),
    )
    expect(prose).toEqual([])
  })

  it('uses a daily chart for long windows', async () => {
    api.getTraffic.mockResolvedValue(base())
    render(<TrafficView token="t" onSignOut={() => {}} />)
    await screen.findByText('独立访客')
    fireEvent.click(screen.getByRole('button', { name: '7d' }))
    expect(await screen.findByText('每日请求数（UTC）')).toBeTruthy()
    expect(screen.getByRole('img', { name: '每日请求数：共 2000 个，其中自动化 1500 个' })).toBeTruthy()
  })

  it('renders the address note from its code, in the dictionary language', async () => {
    api.getTraffic.mockResolvedValue(base())
    render(<TrafficView token="t" onSignOut={() => {}} />)
    const note = await screen.findByText(/地址已做脱敏处理/)
    expect(note.textContent).toContain('类型是根据 User-Agent')
    expect(note.textContent).not.toContain('Server note')
  })

  it('falls back to the server note for an unknown or missing code', async () => {
    api.getTraffic.mockResolvedValue(base({ address_note_code: 'admin.traffic.something_new' }))
    const first = render(<TrafficView token="t" onSignOut={() => {}} />)
    expect((await screen.findByText(/Server note in whatever language\./)).textContent).toContain(
      'Server note',
    )
    first.unmount()
    api.getTraffic.mockResolvedValue(base({ address_note_code: undefined }))
    render(<TrafficView token="t" onSignOut={() => {}} />)
    expect(await screen.findByText(/Server note in whatever language\./)).toBeTruthy()
  })

  it.each([
    ['not_configured', '尚未配置访问日志'],
    ['file_missing', '已设置访问日志路径，但该位置没有文件'],
    ['unreadable', '访问日志存在，但 API 容器无权读取'],
  ] as const)('explains the %s state in Chinese', async (reason, text) => {
    api.getTraffic.mockResolvedValue(disabled(reason))
    const { container } = render(<TrafficView token="t" onSignOut={() => {}} />)
    const panel = await screen.findByRole('status')
    expect(within(panel).getByText('访客流量尚未设置')).toBeTruthy()
    expect(panel.textContent).toContain(text)
    expect(within(panel).getByText('开启方法：')).toBeTruthy()
    expect([...container.querySelectorAll('.admin-traffic-disabled code')].map((c) => c.textContent)).toEqual([
      'caddy-data:/caddy-data:ro',
      'QUBERI_ACCESS_LOG_PATH',
      '/caddy-data/access.log',
      'QUBERI_TRAFFIC_SITE_HOSTS',
      'deploy/compose.prod.yaml',
      'deploy/README.md',
    ])
  })

  it('renders a coded request error in Chinese and a network failure as such', async () => {
    api.getTraffic.mockRejectedValue(
      new AdminApiError(403, 'x', 'admin.auth.sign_in_required'),
    )
    const first = render(<TrafficView token="t" onSignOut={() => {}} />)
    expect(await screen.findByText('请先登录。')).toBeTruthy()
    first.unmount()
    api.getTraffic.mockRejectedValue(new TypeError('Failed to fetch'))
    render(<TrafficView token="t" onSignOut={() => {}} />)
    expect(await screen.findByText('无法连接管理 API，请稍后再试。')).toBeTruthy()
  })

  it('pluralises the log file count in English and not in Chinese', async () => {
    api.getTraffic.mockResolvedValue(base({ files_read: 2 }))
    const zh = render(<TrafficView token="t" onSignOut={() => {}} />)
    expect(await screen.findByText(/已读取 2 个日志文件/)).toBeTruthy()
    zh.unmount()

    act(() => setLang('en'))
    api.getTraffic.mockResolvedValue(base({ files_read: 1 }))
    const one = render(<TrafficView token="t" onSignOut={() => {}} />)
    expect(await screen.findByText(/UTC · 1 log file read/)).toBeTruthy()
    one.unmount()

    api.getTraffic.mockResolvedValue(base({ files_read: 2 }))
    render(<TrafficView token="t" onSignOut={() => {}} />)
    expect(await screen.findByText(/UTC · 2 log files read/)).toBeTruthy()
  })

  it('switches the whole view when the language changes', async () => {
    api.getTraffic.mockResolvedValue(base())
    render(<TrafficView token="t" onSignOut={() => {}} />)
    expect(await screen.findByText('独立访客')).toBeTruthy()
    act(() => setLang('en'))
    await waitFor(() => expect(screen.getByText('Distinct visitors')).toBeTruthy())
  })
})
