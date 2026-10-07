'use client';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { Shield, Plus, Phone } from 'lucide-react';
import { authApi } from '@/lib/api';
import {
  PageHeader,
  Button,
  Card,
  StatusBadge,
  Badge,
  Avatar,
  Sheet,
  Input,
  Label,
  ScoreBar,
  SkeletonList,
  EmptyState,
  useToast,
} from '@/components/ui';

interface TgAccount {
  id: string;
  phone: string;
  username: string;
  status: string;
  trustScore: number;
  healthScore: number;
  floodCount7d: number;
  cluster: string;
  lastUsed: string;
}

const MOCK: TgAccount[] = [
  { id: '1', phone: '+7 912 345-67-80', username: '@account_main', status: 'ACTIVE', trustScore: 92, healthScore: 98, floodCount7d: 0, cluster: 'Cluster A', lastUsed: '2 мин' },
  { id: '2', phone: '+7 987 654-32-10', username: '@account_second', status: 'ACTIVE', trustScore: 85, healthScore: 90, floodCount7d: 2, cluster: 'Cluster A', lastUsed: '15 мин' },
  { id: '3', phone: '+7 916 123-45-67', username: '', status: 'WARNING', trustScore: 55, healthScore: 60, floodCount7d: 8, cluster: 'Cluster B', lastUsed: '3 ч' },
  { id: '4', phone: '+7 903 987-65-43', username: '@account_promo', status: 'LIMITED', trustScore: 32, healthScore: 35, floodCount7d: 24, cluster: 'Cluster B', lastUsed: '1 д' },
  { id: '5', phone: '+7 925 111-22-33', username: '', status: 'DISCONNECTED', trustScore: 0, healthScore: 0, floodCount7d: 0, cluster: '—', lastUsed: '5 дн' },
];

export default function TelegramAccountsPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const [adding, setAdding] = useState(false);
  const [phone, setPhone] = useState('');

  const { data, isLoading, isError } = useQuery<TgAccount[]>({
    queryKey: ['telegram-accounts'],
    queryFn: () => authApi.get('/accounts').then((r: any) => r.data ?? r),
    retry: false,
  });
  const accounts = isError ? MOCK : data ?? [];

  const addAccount = useMutation({
    mutationFn: () => authApi.post('/accounts', { phone }),
    onSuccess: () => {
      setPhone('');
      setAdding(false);
      toast('Аккаунт добавлен — подтвердите вход по коду', 'success');
      qc.invalidateQueries({ queryKey: ['telegram-accounts'] });
    },
    onError: () => toast('Не удалось добавить аккаунт', 'error'),
  });

  const summary = [
    { label: 'Всего', value: accounts.length, tone: 'neutral' as const },
    { label: 'Активных', value: accounts.filter((a) => a.status === 'ACTIVE').length, tone: 'success' as const },
    { label: 'Внимание', value: accounts.filter((a) => a.status === 'WARNING').length, tone: 'warning' as const },
    { label: 'Ограничены', value: accounts.filter((a) => a.status === 'LIMITED').length, tone: 'danger' as const },
  ];

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-4 sm:p-6">
      <PageHeader
        title="Аккаунты"
        subtitle="Мониторинг здоровья и доверия аккаунтов"
        action={
          <Button onClick={() => setAdding(true)}>
            <Plus size={16} /> Добавить
          </Button>
        }
      />

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {summary.map((c) => (
          <Card key={c.label} className="p-4">
            <p className="text-xs font-medium text-fg-muted">{c.label}</p>
            <p
              className="mt-1 text-2xl font-bold tabular-nums"
              style={{ color: c.tone === 'neutral' ? 'var(--fg)' : `var(--${c.tone})` }}
            >
              {c.value}
            </p>
          </Card>
        ))}
      </div>

      {isLoading && !isError ? (
        <SkeletonList rows={5} />
      ) : accounts.length === 0 ? (
        <EmptyState icon={<Shield size={26} />} title="Нет аккаунтов" description="Добавьте первый аккаунт по номеру телефона." />
      ) : (
        <div className="space-y-2.5">
          {accounts.map((a) => (
            <Card key={a.id} className="flex items-center gap-3 p-3.5">
              <Avatar name={a.username || a.phone} size={42} />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <p className="truncate font-semibold text-fg">{a.phone}</p>
                  <StatusBadge status={a.status} />
                </div>
                <p className="truncate text-xs text-fg-hint">
                  {a.username || 'без username'} · {a.cluster} · {a.lastUsed} назад
                </p>
                <div className="mt-2 flex items-center gap-4">
                  <div className="flex items-center gap-1.5">
                    <span className="text-2xs text-fg-hint">Trust</span>
                    <ScoreBar value={a.trustScore} label width="w-12" />
                  </div>
                  <div className="flex items-center gap-1.5">
                    <span className="text-2xs text-fg-hint">Health</span>
                    <ScoreBar value={a.healthScore} label width="w-12" />
                  </div>
                </div>
              </div>
              {a.floodCount7d > 0 && (
                <Badge tone={a.floodCount7d < 10 ? 'warning' : 'danger'}>flood {a.floodCount7d}</Badge>
              )}
            </Card>
          ))}
        </div>
      )}

      <Sheet
        open={adding}
        onClose={() => setAdding(false)}
        title="Добавить аккаунт"
        description="Введите номер телефона — код подтверждения придёт в Telegram"
        footer={
          <>
            <Button variant="secondary" onClick={() => setAdding(false)}>
              Отмена
            </Button>
            <Button onClick={() => addAccount.mutate()} disabled={!phone} loading={addAccount.isPending}>
              Добавить
            </Button>
          </>
        }
      >
        <div className="py-1">
          <Label htmlFor="ph">Телефон</Label>
          <div className="relative">
            <Phone size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-fg-hint" />
            <Input id="ph" value={phone} onChange={(e) => setPhone(e.target.value)} placeholder="+7 900 000-00-00" className="pl-9" />
          </div>
        </div>
      </Sheet>
    </div>
  );
}
