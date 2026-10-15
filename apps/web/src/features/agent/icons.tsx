import {
  Bot,
  BookOpen,
  Briefcase,
  Compass,
  Headphones,
  Scale,
  Sparkles,
  Users,
  Wrench,
} from 'lucide-react'
import type { ComponentType } from 'react'

/**
 * Agent icon registry. The backend stores an opaque icon *key*; the
 * frontend resolves it to a glyph here, so adding an icon never needs a
 * migration. Unknown keys fall back to the generic Bot.
 */

type IconComponent = ComponentType<{ size?: number; strokeWidth?: number }>

const ICONS: Record<string, IconComponent> = {
  bot: Bot,
  sparkles: Sparkles,
  users: Users,
  book: BookOpen,
  wrench: Wrench,
  briefcase: Briefcase,
  scale: Scale,
  compass: Compass,
  support: Headphones,
}

/** Ordered keys offered in the editor's icon picker. */
export const AGENT_ICON_KEYS: string[] = [
  'bot',
  'sparkles',
  'users',
  'book',
  'wrench',
  'briefcase',
  'scale',
  'compass',
  'support',
]

export function AgentIcon({
  icon,
  size = 18,
  strokeWidth = 1.8,
}: {
  icon: string
  size?: number
  strokeWidth?: number
}) {
  const Cmp = ICONS[icon] ?? Bot
  return <Cmp size={size} strokeWidth={strokeWidth} />
}
