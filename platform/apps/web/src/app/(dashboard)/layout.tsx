'use client';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import {
  MessageSquare, Users, BarChart2, Send, Bot, Settings, LogOut,
  LayoutDashboard, Zap, MessagesSquare, Server, Eye, Cog,
  Shield, Network, Radio, Wifi, Activity, Search, TrendingUp,
  UserCheck, Bell, Play, PlusCircle, List, Clock, FileText, BookOpen,
} from 'lucide-react';
import clsx from 'clsx';
import { useState, useEffect } from 'react';

type Mode = 'infrastructure' | 'visibility' | 'operations';

const INFRASTRUCTURE_NAV = [
  { href: '/dashboard', label: 'Обзор', icon: LayoutDashboard },
  { href: '/assets', label: 'Активы', icon: Server },
  { href: '/telegram-accounts', label: 'Аккаунты', icon: Shield },
  { href: '/bots', label: 'Боты', icon: Bot },
  { href: '/clusters', label: 'Кластеры', icon: Network },
  { href: '/proxies', label: 'Прокси', icon: Wifi },
  { href: '/channels', label: 'Каналы', icon: Radio },
  { href: '/health', label: 'Здоровье', icon: Activity },
];

const VISIBILITY_NAV = [
  { href: '/visibility', label: 'Дашборд', icon: Eye },
  { href: '/visibility/keywords', label: 'Ключевые слова', icon: Search },
  { href: '/visibility/rankings', label: 'Позиции', icon: TrendingUp },
  { href: '/visibility/competitors', label: 'Конкуренты', icon: UserCheck },
  { href: '/visibility/trends', label: 'Тренды', icon: BarChart2 },
  { href: '/visibility/alerts', label: 'Алерты', icon: Bell },
];

const OPERATIONS_NAV = [
  { href: '/operations', label: 'Дашборд', icon: LayoutDashboard },
  { href: '/operations/new', label: 'Создать операцию', icon: PlusCircle },
  { href: '/operations/queue', label: 'Очередь', icon: List },
  { href: '/operations/history', label: 'История', icon: Clock },
  { href: '/operations/templates', label: 'Шаблоны', icon: FileText },
  { href: '/operations/audit', label: 'Аудит', icon: BookOpen },
];

const CRM_NAV = [
  { href: '/inbox', label: 'Входящие', icon: MessageSquare },
  { href: '/conversations', label: 'Разговоры', icon: MessagesSquare },
  { href: '/users', label: 'Пользователи', icon: Users },
  { href: '/broadcasts', label: 'Рассылки', icon: Send },
  { href: '/analytics', label: 'Аналитика', icon: BarChart2 },
  { href: '/automations', label: 'Автоматизации', icon: Zap },
  { href: '/settings', label: 'Настройки', icon: Settings },
];

const MODES = [
  { key: 'infrastructure' as Mode, label: 'Infrastructure', icon: '🏗️' },
  { key: 'visibility' as Mode, label: 'Visibility', icon: '👁️' },
  { key: 'operations' as Mode, label: 'Operations', icon: '⚙️' },
];

function NavSection({ items, path }: { items: typeof INFRASTRUCTURE_NAV; path: string }) {
  return (
    <>
      {items.map(({ href, label, icon: Icon }) => (
        <Link
          key={href}
          href={href}
          className={clsx(
            'flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium transition-colors',
            path === href || (path.startsWith(href) && href !== '/dashboard' && href !== '/visibility' && href !== '/operations')
              ? 'bg-sky-50 text-sky-700'
              : path === href
              ? 'bg-sky-50 text-sky-700'
              : 'text-slate-600 hover:bg-slate-50 hover:text-slate-800',
          )}
        >
          <Icon size={16} />
          {label}
        </Link>
      ))}
    </>
  );
}

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const path = usePathname();
  const [mode, setMode] = useState<Mode>('infrastructure');

  useEffect(() => {
    const stored = localStorage.getItem('bm_mode') as Mode | null;
    if (stored && ['infrastructure', 'visibility', 'operations'].includes(stored)) {
      setMode(stored);
    }
  }, []);

  const switchMode = (m: Mode) => {
    setMode(m);
    localStorage.setItem('bm_mode', m);
  };

  const currentNav =
    mode === 'infrastructure'
      ? INFRASTRUCTURE_NAV
      : mode === 'visibility'
      ? VISIBILITY_NAV
      : OPERATIONS_NAV;

  return (
    <div className="flex h-screen bg-slate-50">
      {/* Sidebar */}
      <aside className="w-56 bg-white border-r border-slate-200 flex flex-col">
        {/* Logo */}
        <div className="p-4 border-b border-slate-100">
          <span className="font-bold text-slate-800 text-lg">TG Platform</span>
        </div>

        {/* Mode Switcher */}
        <div className="px-2 py-2 border-b border-slate-100">
          <p className="text-[10px] font-semibold text-slate-400 uppercase tracking-wider px-1 mb-1.5">Режим</p>
          <div className="flex gap-1">
            {MODES.map(({ key, label, icon }) => (
              <button
                key={key}
                onClick={() => switchMode(key)}
                title={label}
                className={clsx(
                  'flex-1 flex flex-col items-center py-1.5 rounded-lg text-[10px] font-medium transition-colors gap-0.5',
                  mode === key
                    ? 'bg-sky-50 text-sky-700 border border-sky-200'
                    : 'text-slate-500 hover:bg-slate-50 border border-transparent',
                )}
              >
                <span className="text-base leading-none">{icon}</span>
                <span className="truncate w-full text-center leading-none">{label.split('').slice(0, 5).join('')}</span>
              </button>
            ))}
          </div>
        </div>

        {/* Main Navigation */}
        <nav className="flex-1 py-2 space-y-0.5 px-2 overflow-y-auto">
          <NavSection items={currentNav} path={path} />

          {/* CRM section always visible in infrastructure mode, or as collapsed section */}
          {mode === 'infrastructure' && (
            <>
              <div className="pt-3 pb-1">
                <p className="text-[10px] font-semibold text-slate-400 uppercase tracking-wider px-2">CRM</p>
              </div>
              <NavSection items={CRM_NAV} path={path} />
            </>
          )}
        </nav>

        {/* Logout */}
        <div className="p-2 border-t border-slate-100">
          <button
            onClick={() => { localStorage.clear(); window.location.href = '/login'; }}
            className="flex items-center gap-3 px-3 py-2 rounded-lg text-sm text-slate-500 hover:text-red-500 w-full"
          >
            <LogOut size={16} /> Выйти
          </button>
        </div>
      </aside>

      {/* Main */}
      <main className="flex-1 overflow-auto">{children}</main>
    </div>
  );
}
