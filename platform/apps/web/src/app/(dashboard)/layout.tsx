'use client';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { MessageSquare, Users, BarChart2, Send, Bot, Settings, LogOut } from 'lucide-react';
import clsx from 'clsx';

const NAV = [
  { href: '/inbox', label: 'Входящие', icon: MessageSquare },
  { href: '/users', label: 'CRM', icon: Users },
  { href: '/broadcasts', label: 'Рассылки', icon: Send },
  { href: '/analytics', label: 'Аналитика', icon: BarChart2 },
  { href: '/bots', label: 'Боты', icon: Bot },
  { href: '/settings', label: 'Настройки', icon: Settings },
];

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const path = usePathname();
  return (
    <div className="flex h-screen bg-slate-50">
      {/* Sidebar */}
      <aside className="w-56 bg-white border-r border-slate-200 flex flex-col">
        <div className="p-4 border-b border-slate-100">
          <span className="font-bold text-slate-800 text-lg">TG Platform</span>
        </div>
        <nav className="flex-1 py-2 space-y-0.5 px-2">
          {NAV.map(({ href, label, icon: Icon }) => (
            <Link
              key={href}
              href={href}
              className={clsx(
                'flex items-center gap-3 px-3 py-2 rounded-lg text-sm font-medium transition-colors',
                path.startsWith(href)
                  ? 'bg-sky-50 text-sky-700'
                  : 'text-slate-600 hover:bg-slate-50 hover:text-slate-800',
              )}
            >
              <Icon size={16} />
              {label}
            </Link>
          ))}
        </nav>
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
