import {
  LayoutDashboard,
  Server,
  Shield,
  Bot,
  Network,
  Wifi,
  Radio,
  Activity,
  Eye,
  Search,
  TrendingUp,
  UserCheck,
  BarChart2,
  Bell,
  PlusCircle,
  List,
  Clock,
  FileText,
  BookOpen,
  MessageSquare,
  MessagesSquare,
  Users,
  Send,
  Zap,
  Settings,
  Gauge,
  type LucideIcon,
} from 'lucide-react';

export interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
}

export interface NavGroup {
  key: string;
  label: string;
  emoji: string;
  items: NavItem[];
}

export const NAV_GROUPS: NavGroup[] = [
  {
    key: 'infrastructure',
    label: 'Инфраструктура',
    emoji: '🏗️',
    items: [
      { href: '/dashboard', label: 'Обзор', icon: LayoutDashboard },
      { href: '/assets', label: 'Активы', icon: Server },
      { href: '/telegram-accounts', label: 'Аккаунты', icon: Shield },
      { href: '/bots', label: 'Боты', icon: Bot },
      { href: '/clusters', label: 'Кластеры', icon: Network },
      { href: '/proxies', label: 'Прокси', icon: Wifi },
      { href: '/channels', label: 'Каналы', icon: Radio },
      { href: '/health', label: 'Здоровье', icon: Activity },
    ],
  },
  {
    key: 'operations',
    label: 'Операции',
    emoji: '⚙️',
    items: [
      { href: '/operations', label: 'Дашборд', icon: Gauge },
      { href: '/operations/new', label: 'Создать', icon: PlusCircle },
      { href: '/operations/queue', label: 'Очередь', icon: List },
      { href: '/operations/history', label: 'История', icon: Clock },
      { href: '/operations/templates', label: 'Шаблоны', icon: FileText },
      { href: '/operations/audit', label: 'Аудит', icon: BookOpen },
    ],
  },
  {
    key: 'visibility',
    label: 'Видимость',
    emoji: '👁️',
    items: [
      { href: '/visibility', label: 'Дашборд', icon: Eye },
      { href: '/visibility/keywords', label: 'Ключевые слова', icon: Search },
      { href: '/visibility/rankings', label: 'Позиции', icon: TrendingUp },
      { href: '/visibility/competitors', label: 'Конкуренты', icon: UserCheck },
      { href: '/visibility/trends', label: 'Тренды', icon: BarChart2 },
      { href: '/visibility/alerts', label: 'Алерты', icon: Bell },
    ],
  },
  {
    key: 'crm',
    label: 'CRM и общение',
    emoji: '💬',
    items: [
      { href: '/inbox', label: 'Входящие', icon: MessageSquare },
      { href: '/conversations', label: 'Разговоры', icon: MessagesSquare },
      { href: '/users', label: 'Пользователи', icon: Users },
      { href: '/broadcasts', label: 'Рассылки', icon: Send },
      { href: '/automations', label: 'Автоматизации', icon: Zap },
      { href: '/analytics', label: 'Аналитика', icon: BarChart2 },
      { href: '/settings', label: 'Настройки', icon: Settings },
    ],
  },
];

/** Primary bottom-tab destinations (mobile). The last tab opens the full nav. */
export const BOTTOM_TABS: NavItem[] = [
  { href: '/dashboard', label: 'Обзор', icon: LayoutDashboard },
  { href: '/assets', label: 'Активы', icon: Server },
  { href: '/operations', label: 'Операции', icon: Gauge },
  { href: '/inbox', label: 'Входящие', icon: MessageSquare },
];

const ALL_ITEMS = NAV_GROUPS.flatMap((g) => g.items);

/** Longest-prefix match so sub-routes highlight their parent correctly. */
export function activeHref(path: string): string | null {
  let best: string | null = null;
  for (const it of ALL_ITEMS) {
    if (path === it.href || path.startsWith(it.href + '/')) {
      if (!best || it.href.length > best.length) best = it.href;
    }
  }
  return best;
}

export function titleFor(path: string): string {
  const href = activeHref(path);
  return ALL_ITEMS.find((i) => i.href === href)?.label ?? 'Infragram';
}
