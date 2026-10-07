'use client';
import { TrendingUp, TrendingDown, Search, Eye, ArrowUp } from 'lucide-react';
import { PageHeader, Card, Stat } from '@/components/ui';

const METRICS = [
  { label: 'Ключевых слов', value: '248', icon: <Search size={17} />, tone: 'accent' as const, delta: 12, deltaLabel: 'за неделю' },
  { label: 'Средняя позиция', value: '4.2', icon: <TrendingUp size={17} />, tone: 'success' as const },
  { label: 'Растут', value: '34', icon: <ArrowUp size={17} />, tone: 'violet' as const },
  { label: 'Падают', value: '11', icon: <TrendingDown size={17} />, tone: 'danger' as const },
];

const GROWING = [
  { keyword: 'telegram каналы каталог', position: 2, delta: 3, group: 'Каталог' },
  { keyword: 'telegram прокси', position: 1, delta: 5, group: 'Прокси' },
  { keyword: 'telegram bot api', position: 3, delta: 2, group: 'API' },
  { keyword: 'telegram channel analytics', position: 4, delta: 4, group: 'Аналитика' },
  { keyword: 'telegram automation', position: 2, delta: 6, group: 'Автоматизация' },
];

const FALLING = [
  { keyword: 'telegram crm', position: 8, delta: -3, group: 'CRM' },
  { keyword: 'telegram рассылка', position: 12, delta: -5, group: 'Рассылки' },
  { keyword: 'telegram scheduler', position: 15, delta: -2, group: 'Планировщик' },
];

function RankList({
  title,
  items,
  up,
}: {
  title: string;
  items: { keyword: string; position: number; delta: number; group: string }[];
  up: boolean;
}) {
  return (
    <Card>
      <div className="flex items-center gap-2 border-b border-line px-4 py-3.5">
        {up ? <TrendingUp size={16} className="text-success" /> : <TrendingDown size={16} className="text-danger" />}
        <h2 className="text-sm font-semibold text-fg">{title}</h2>
      </div>
      <div className="divide-y divide-line">
        {items.map((it) => (
          <div key={it.keyword} className="flex items-center gap-3 px-4 py-3">
            <span
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-xs font-bold"
              style={{
                background: up ? 'var(--success-weak)' : 'var(--danger-weak)',
                color: up ? 'var(--success)' : 'var(--danger)',
              }}
            >
              {it.position}
            </span>
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium text-fg">{it.keyword}</p>
              <p className="text-2xs text-fg-hint">{it.group}</p>
            </div>
            <span
              className="flex items-center gap-0.5 text-sm font-semibold"
              style={{ color: up ? 'var(--success)' : 'var(--danger)' }}
            >
              {up ? <TrendingUp size={13} /> : <TrendingDown size={13} />}
              {up ? '+' : ''}
              {it.delta}
            </span>
          </div>
        ))}
      </div>
    </Card>
  );
}

export default function VisibilityPage() {
  return (
    <div className="mx-auto max-w-5xl space-y-5 p-4 sm:p-6">
      <PageHeader title="Видимость" subtitle="Позиции в поиске Telegram и динамика" />

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {METRICS.map((m) => (
          <Stat key={m.label} label={m.label} value={m.value} icon={m.icon} tone={m.tone} delta={m.delta} deltaLabel={m.deltaLabel} />
        ))}
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <RankList title="Топ растущих" items={GROWING} up />
        <RankList title="Топ падающих" items={FALLING} up={false} />
      </div>

      <div className="flex items-center gap-3 rounded-2xl bg-accent-weak px-4 py-3">
        <Eye size={16} className="shrink-0 text-accent" />
        <p className="text-sm text-accent">Демо-данные. Реальные позиции подтянутся после подключения трекинга.</p>
      </div>
    </div>
  );
}
