/**
 * Icons for the tools that may ask for access, by the id Quberi registered
 * them under. A tool with no entry gets a generic icon.
 *
 * These are the tools' own marks, shown only to say which tool is asking.
 */
export const CLIENT_ICONS: Readonly<Record<string, string>> = {
  'quberi-harness': '/clients/deepseek-harness.png',
}

export function clientIcon(clientId: string): string | undefined {
  return Object.prototype.hasOwnProperty.call(CLIENT_ICONS, clientId) ? CLIENT_ICONS[clientId] : undefined
}
