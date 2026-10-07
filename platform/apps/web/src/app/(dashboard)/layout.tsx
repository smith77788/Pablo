'use client';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useEffect, useMemo, useState } from 'react';
import { ChevronLeft, LayoutGrid, LogOut, X, type LucideIcon } from 'lucide-react';
import { cn } from '@/components/ui';
import { NAV_GROUPS, BOTTOM_TABS, activeHref, titleFor } from '@/lib/nav';
import { bindBackButton, haptic } from '@/lib/telegram';

function Logo() {
  return (
    <div className="flex items-center gap-2.5">
      <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-accent text-accent-fg font-bold shadow-sm">
        i
      </span>
      <span className="text-[15px] font-bold tracking-tight text-fg">Infragram</span>
    </div>
  );
}

function NavLink({
  href,
  label,
  Icon,
  active,
  onClick,
}: {
  href: string;
  label: string;
  Icon: LucideIcon;
  active: boolean;
  onClick?: () => void;
}) {
  return (
    <Link
      href={href}
      onClick={onClick}
      className={cn(
        'flex items-center gap-3 rounded-xl px-3 py-2 text-sm font-medium transition-colors',
        active ? 'bg-accent-weak text-accent' : 'text-fg-muted hover:bg-surface-2 hover:text-fg',
      )}
    >
      <Icon size={17} />
      {label}
    </Link>
  );
}

function logout() {
  localStorage.clear();
  window.location.href = '/login';
}

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const path = usePathname();
  const router = useRouter();
  const active = activeHref(path);
  const [moreOpen, setMoreOpen] = useState(false);

  const currentGroup = useMemo(
    () => NAV_GROUPS.find((g) => g.items.some((i) => i.href === active)),
    [active],
  );

  const isRoot = path === '/dashboard';

  // Native Telegram back button on sub-screens
  useEffect(() => {
    return bindBackButton(!isRoot, () => router.back());
  }, [isRoot, router]);

  useEffect(() => {
    setMoreOpen(false);
  }, [path]);

  return (
    <div className="flex min-h-screen bg-bg">
      {/* ===== Desktop sidebar ===== */}
      <aside className="sticky top-0 hidden h-screen w-64 shrink-0 flex-col border-r border-line bg-surface md:flex">
        <div className="px-4 py-4">
          <Logo />
        </div>
        <nav className="flex-1 space-y-5 overflow-y-auto px-3 py-2">
          {NAV_GROUPS.map((g) => (
            <div key={g.key}>
              <p className="px-3 pb-1.5 text-2xs font-semibold uppercase tracking-wider text-fg-hint">
                {g.emoji} {g.label}
              </p>
              <div className="space-y-0.5">
                {g.items.map((i) => (
                  <NavLink
                    key={i.href}
                    href={i.href}
                    label={i.label}
                    Icon={i.icon}
                    active={active === i.href}
                  />
                ))}
              </div>
            </div>
          ))}
        </nav>
        <div className="border-t border-line p-3">
          <button
            onClick={logout}
            className="flex w-full items-center gap-3 rounded-xl px-3 py-2 text-sm font-medium text-fg-muted transition-colors hover:bg-danger-weak hover:text-danger"
          >
            <LogOut size={17} /> Выйти
          </button>
        </div>
      </aside>

      {/* ===== Main column ===== */}
      <div className="flex min-w-0 flex-1 flex-col">
        {/* Top app bar */}
        <header className="sticky top-0 z-30 border-b border-line bg-surface/85 backdrop-blur-md pt-safe">
          <div className="flex h-14 items-center gap-2 px-3 sm:px-5">
            {!isRoot ? (
              <button
                onClick={() => {
                  haptic('light');
                  router.back();
                }}
                className="-ml-1 rounded-lg p-1.5 text-fg-muted hover:bg-surface-2 hover:text-fg md:hidden"
                aria-label="Назад"
              >
                <ChevronLeft size={22} />
              </button>
            ) : (
              <div className="md:hidden">
                <Logo />
              </div>
            )}
            <h1 className="flex-1 truncate text-center text-base font-semibold text-fg md:text-left">
              {isRoot ? '' : titleFor(path)}
            </h1>
            <button
              onClick={logout}
              className="rounded-lg p-1.5 text-fg-hint hover:bg-surface-2 hover:text-fg md:hidden"
              aria-label="Выйти"
            >
              <LogOut size={19} />
            </button>
          </div>

          {/* Section sub-nav strip */}
          {currentGroup && currentGroup.items.length > 1 && (
            <div className="no-scrollbar flex gap-1.5 overflow-x-auto px-3 pb-2.5 sm:px-5">
              {currentGroup.items.map((i) => {
                const on = active === i.href;
                return (
                  <Link
                    key={i.href}
                    href={i.href}
                    className={cn(
                      'flex items-center gap-1.5 whitespace-nowrap rounded-lg px-3 py-1.5 text-xs font-medium transition-colors',
                      on
                        ? 'bg-accent text-accent-fg shadow-sm'
                        : 'bg-surface-2 text-fg-muted hover:text-fg',
                    )}
                  >
                    <i.icon size={13} />
                    {i.label}
                  </Link>
                );
              })}
            </div>
          )}
        </header>

        <main className="flex-1 pb-[calc(var(--tabbar-h)+env(safe-area-inset-bottom,0px)+8px)] md:pb-0">
          {children}
        </main>
      </div>

      {/* ===== Mobile bottom tab bar ===== */}
      <nav className="fixed inset-x-0 bottom-0 z-30 h-tabbar border-t border-line bg-surface/90 backdrop-blur-md md:hidden">
        <div className="grid h-[var(--tabbar-h)] grid-cols-5">
          {BOTTOM_TABS.map((t) => {
            const on = active === t.href;
            return (
              <Link
                key={t.href}
                href={t.href}
                onClick={() => haptic('select')}
                className={cn(
                  'flex flex-col items-center justify-center gap-1 text-2xs font-medium transition-colors',
                  on ? 'text-accent' : 'text-fg-hint',
                )}
              >
                <t.icon size={21} />
                {t.label}
              </Link>
            );
          })}
          <button
            onClick={() => {
              haptic('light');
              setMoreOpen(true);
            }}
            className={cn(
              'flex flex-col items-center justify-center gap-1 text-2xs font-medium transition-colors',
              moreOpen ? 'text-accent' : 'text-fg-hint',
            )}
          >
            <LayoutGrid size={21} />
            Ещё
          </button>
        </div>
      </nav>

      {/* ===== Full navigation sheet (mobile "More") ===== */}
      {moreOpen && (
        <div className="fixed inset-0 z-50 flex items-end md:hidden">
          <div className="absolute inset-0 bg-black/50 animate-fade-in" onClick={() => setMoreOpen(false)} />
          <div className="relative max-h-[85vh] w-full overflow-y-auto rounded-t-3xl bg-surface shadow-lg animate-sheet-up pb-safe">
            <div className="sticky top-0 flex items-center justify-between border-b border-line bg-surface px-5 py-3.5">
              <span className="text-base font-semibold text-fg">Навигация</span>
              <button
                onClick={() => setMoreOpen(false)}
                className="rounded-lg p-1.5 text-fg-hint hover:bg-surface-2"
                aria-label="Закрыть"
              >
                <X size={18} />
              </button>
            </div>
            <div className="space-y-5 px-4 py-4">
              {NAV_GROUPS.map((g) => (
                <div key={g.key}>
                  <p className="px-1 pb-2 text-2xs font-semibold uppercase tracking-wider text-fg-hint">
                    {g.emoji} {g.label}
                  </p>
                  <div className="grid grid-cols-2 gap-2">
                    {g.items.map((i) => {
                      const on = active === i.href;
                      return (
                        <Link
                          key={i.href}
                          href={i.href}
                          onClick={() => haptic('select')}
                          className={cn(
                            'flex items-center gap-2.5 rounded-xl border px-3 py-2.5 text-sm font-medium transition-colors',
                            on
                              ? 'border-accent/30 bg-accent-weak text-accent'
                              : 'border-line bg-surface-2 text-fg hover:border-line-strong',
                          )}
                        >
                          <i.icon size={17} />
                          <span className="truncate">{i.label}</span>
                        </Link>
                      );
                    })}
                  </div>
                </div>
              ))}
              <button
                onClick={logout}
                className="flex w-full items-center justify-center gap-2 rounded-xl border border-line py-3 text-sm font-medium text-danger hover:bg-danger-weak"
              >
                <LogOut size={17} /> Выйти
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
