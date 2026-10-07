'use client';
import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { Server, Search, Bot, Shield, Radio, Wifi } from 'lucide-react';
import { authApi } from '@/lib/api';
import {
  PageHeader,
  Card,
  StatusBadge,
  Badge,
  Segmented,
  Input,
  ScoreBar,
  SkeletonList,
  EmptyState,
  type Tone,
} from '@/components/ui';

interface Asset {
  id: string;
  name: string;
  type: string;
  status: string;
  healthScore: number;
  cluster: string;
  lastActivity: string;
}

const MOCK: Asset[] = [
  { id: '1', name: '@main_bot', type: 'BOT', status: 'ACTIVE', healthScore: 98, cluster: 'Cluster A', lastActivity: '2 мин' },
  { id: '2', name: '+7 912 345-67-80', type: 'ACCOUNT', status: 'ACTIVE', healthScore: 92, cluster: 'Cluster A', lastActivity: '5 мин' },
  { id: '3', name: '185.12.45.67:3128', type: 'PROXY', status: 'ACTIVE', healthScore: 87, cluster: 'Cluster B', lastActivity: '1 мин' },
  { id: '4', name: '@news_channel', type: 'CHANNEL', status: 'ACTIVE', healthScore: 100, cluster: 'Cluster A', lastActivity: '10 мин' },
  { id: '5', name: '@promo_account', type: 'ACCOUNT', status: 'WARNING', healthScore: 58, cluster: 'Cluster B', lastActivity: '3 ч' },
  { id: '6', name: '@deals_channel', type: 'CHANNEL', status: 'ACTIVE', healthScore: 94, cluster: 'Cluster A', lastActivity: '22 мин' },
  { id: '7', name: '91.234.56.78:1080', type: 'PROXY', status: 'LIMITED', healthScore: 30, cluster: 'Cluster B', lastActivity: '1 д' },
];

const TYPE_META: Record<string, { tone: Tone; label: string; Icon: typeof Bot }> = {
  ACCOUNT: { tone: 'accent', label: 'Аккаунт', Icon: Shield },
  BOT: { tone: 'violet', label: 'Бот', Icon: Bot },
  CHANNEL: { tone: 'warning', label: 'Канал', Icon: Radio },
  PROXY: { tone: 'success', label: 'Прокси', Icon: Wifi },
};

type Filter = 'ALL' | 'ACCOUNT' | 'BOT' | 'CHANNEL' | 'PROXY';

export default function AssetsPage() {
  const [filter, setFilter] = useState<Filter>('ALL');
  const [q, setQ] = useState('');

  const { data, isLoading, isError } = useQuery<Asset[]>({
    queryKey: ['assets'],
    queryFn: () => authApi.get('/assets').then((r: any) => r.data ?? r),
    retry: false,
  });
  const assets = isError ? MOCK : data ?? [];

  const filtered = useMemo(
    () =>
      assets.filter(
        (a) =>
          (filter === 'ALL' || a.type === filter) &&
          (!q || a.name.toLowerCase().includes(q.toLowerCase())),
      ),
    [assets, filter, q],
  );

  const count = (t: Filter) => (t === 'ALL' ? assets.length : assets.filter((a) => a.type === t).length);

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-4 sm:p-6">
      <PageHeader title="Активы" subtitle="Единый реестр всей инфраструктуры" />

      <div className="relative">
        <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-fg-hint" />
        <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Поиск по названию…" className="pl-9" />
      </div>

      <Segmented
        value={filter}
        onChange={setFilter}
        options={[
          { value: 'ALL', label: 'Все', count: count('ALL') },
          { value: 'ACCOUNT', label: 'Аккаунты', count: count('ACCOUNT') },
          { value: 'BOT', label: 'Боты', count: count('BOT') },
          { value: 'CHANNEL', label: 'Каналы', count: count('CHANNEL') },
          { value: 'PROXY', label: 'Прокси', count: count('PROXY') },
        ]}
      />

      {isLoading && !isError ? (
        <SkeletonList rows={6} />
      ) : filtered.length === 0 ? (
        <EmptyState icon={<Server size={26} />} title="Ничего не найдено" description="Измените фильтр или поисковый запрос." />
      ) : (
        <div className="space-y-2.5">
          {filtered.map((a) => {
            const meta = TYPE_META[a.type] ?? { tone: 'neutral' as Tone, label: a.type, Icon: Server };
            return (
              <Card key={a.id} className="flex items-center gap-3 p-3.5">
                <span
                  className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl"
                  style={{ background: `var(--${meta.tone}-weak)`, color: `var(--${meta.tone})` }}
                >
                  <meta.Icon size={18} />
                </span>
                <div className="min-w-0 flex-1">
                  <p className="truncate font-semibold text-fg">{a.name}</p>
                  <p className="truncate text-xs text-fg-hint">
                    {meta.label} · {a.cluster} · {a.lastActivity} назад
                  </p>
                </div>
                <div className="flex flex-col items-end gap-1.5">
                  <StatusBadge status={a.status} />
                  <ScoreBar value={a.healthScore} label width="w-12" />
                </div>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}
