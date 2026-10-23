import type { Entry, MessageKey } from './index'

/** Simplified Chinese strings. Typed against `en.ts`: every key must exist, no extras. */
export const zh: Record<MessageKey, Entry> = {
  // App
  'app.title': 'Quberi 平台管理',
  'app.brand': 'Quberi 管理',
  'app.loadingStats': '正在加载平台统计…',

  // Language switch
  'lang.label': '语言',
  'lang.en': 'English',
  'lang.zh': '简体中文',

  // Shared
  'common.refresh': '刷新',
  'common.loading': '加载中…',
  'common.cancel': '取消',
  'common.save': '保存',
  'common.saving': '保存中…',
  'common.close': '关闭',
  'common.edit': '编辑',
  'common.delete': '删除',
  'common.remove': '移除',
  'common.none': '（无）',

  // Navigation
  'nav.overview': '概览',
  'nav.organizations': '组织',
  'nav.models': '模型',
  'nav.clients': '外部工具',
  'nav.traffic': '访客流量',
  'nav.signOut': '退出登录',

  // Sign-in gate
  'gate.signIn.hint':
    '请使用平台管理员账号登录。全新部署时，首个密码只会在 API 日志中输出一次。',
  'gate.signIn.username': '用户名',
  'gate.signIn.password': '密码',
  'gate.signIn.submit': '登录',
  'gate.signIn.busy': '登录中…',
  'gate.signIn.tooMany': '尝试次数过多，请等待几分钟后再试。',
  'gate.signIn.invalid': '用户名或密码不正确。',
  'gate.signIn.unreachable': '无法连接管理 API，请稍后再试。',

  // Forced password change
  'gate.password.title': '请设置新密码',
  'gate.password.hint': '此账号仍在使用系统分配的密码。请设置你自己的密码（{min} 到 {max} 个字符）后继续。',
  'gate.password.current': '当前密码',
  'gate.password.new': '新密码',
  'gate.password.confirm': '确认新密码',
  'gate.password.mismatch': '两次输入的新密码不一致。',
  'gate.password.failed': '无法修改密码，请重试。',
  'gate.password.submit': '修改密码',

  // Request errors
  'error.generic': '出了点问题，请重试。',
  'error.server': '服务器遇到问题，请稍后再试。',
  'error.network': '无法连接管理 API，请稍后再试。',
  'error.validation': '填写的部分内容无效。',
  'error.status': '请求失败，状态码 {status}',

  // Backend message codes
  'msg.admin.auth.invalid_token': '平台管理员令牌无效。',
  'msg.admin.auth.sign_in_required': '请先登录。',
  'msg.admin.auth.password_too_short': '密码至少需要 {min} 个字符。',
  'msg.admin.auth.password_too_long': '密码最多 {max} 个字符。',
  'msg.admin.auth.password_edge_spaces': '密码开头和结尾不能有空格。',
  'msg.admin.auth.password_repetitive': '密码过于重复。',
  'msg.admin.auth.password_has_username': '密码不能包含用户名。',
  'msg.admin.auth.try_again_shortly': '请稍后重试。',
  'msg.admin.auth.too_many_attempts': '尝试次数过多，请稍后再试。',
  'msg.admin.auth.bad_credentials': '用户名或密码不正确。',
  'msg.admin.auth.current_password_wrong': '当前密码不正确。',
  'msg.admin.auth.password_unchanged': '请选择与当前密码不同的新密码。',
  'msg.admin.oauth.client_id_taken': '已配置了使用该客户端 ID 的外部工具。',
  'msg.admin.oauth.tool_not_found': '找不到该外部工具。',
  'msg.admin.oauth.unknown_kind': '未知的类型。',
  'msg.admin.oauth.registered_id_format':
    '已注册的客户端 ID 由 1 到 64 个字母、数字、点、短横线或下划线组成。',
  'msg.admin.oauth.url_too_long': '{subject}过长。',
  'msg.admin.oauth.url_wildcard_space': '{subject}不能包含通配符或空格。',
  'msg.admin.oauth.url_placeholder_not_allowed': '{subject}不能包含 {placeholder}。',
  'msg.admin.oauth.url_placeholder_once': '{subject}中的 {placeholder} 最多出现一次。',
  'msg.admin.oauth.url_braces': '{subject}包含 {placeholder} 以外的花括号。',
  'msg.admin.oauth.url_invalid': '{subject}不是有效的地址。',
  'msg.admin.oauth.url_not_https': '{subject}必须是 https:// 地址。',
  'msg.admin.oauth.url_extras': '{subject}不能包含账号凭据、查询参数或片段。',
  'msg.admin.oauth.url_port': '{subject}不能指定端口。',
  'msg.admin.oauth.url_domain': '{subject}必须使用真实域名，不能使用 IP 地址。',
  'msg.admin.oauth.url_placeholder_path': '{subject}：{placeholder} 只能出现在路径中。',
  'msg.admin.oauth.redirect_needs_id':
    '回调地址 {rule} 使用了 {placeholder}，但客户端 ID 中没有。',
  'msg.admin.oauth.redirect_required': '至少需要一条回调规则。',
  'msg.admin.oauth.subject.client_id_url': '客户端 ID 网址',
  'msg.admin.oauth.subject.redirect': '回调地址 {rule}',
  'msg.admin.models.binding_not_found': '找不到该平台密钥。',
  'msg.admin.models.visibility_mode_invalid': '模型可见性模式 {mode} 无效。',
  'msg.admin.models.org_not_found': '找不到组织 {id}。',
  'msg.admin.orgs.not_found': '找不到组织：{id}',
  'msg.admin.traffic.address_note':
    "地址已做脱敏处理。取自请求头的访客地址，可被绕过 CDN、直接访问服务器的人伪造；标记为 'remote_ip' 的行使用的是连接地址。",
}
