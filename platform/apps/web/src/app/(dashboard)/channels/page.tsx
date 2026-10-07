'use client';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { Radio, Plus, Users2, Eye, TrendingUp } from 'lucide-react';
import { authApi } from '@/lib/api';
import {
  PageHeader,
  Button,
  Card,
  StatusBadge,
  Avatar,
  Sheet,
  Input,
  Label,
  SkeletonList,
  EmptyState,
  useToast,
} from '@/components/ui';

interface Channel {
  id: string;
  title: string;
  username: string;
  subscribers: number;
  dailyViews: number;
  growth7d: number;
  status: string;
}

const MOCK: Channel[] = [
  { id: '1', title: 'Новости сети', username: '@news_channel', subscribers: 48230, dailyViews: 12400, growth7d: 3.2, status: 'ACTIVE' },
  { id: '2', title: 'Акции и скидки', username: '@deals_channel', subscribers: 21870, dailyViews: 8100, growth7d: 5.8, status: 'ACTIVE' },
  { id: '3', title: 'Обзоры', username: '@reviews_hub', subscribers: 9540, dailyViews: 2300, growth7d: -1.1, status: 'WARNING' },
  { id: '4', title: 'Анонсы', username: '@announce_bot', subscribers: 3120, dailyViews: 640, growth7d: 0.4, status: 'ACTIVE' },
];

export default function ChannelsPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState({ title: '', username: '' });

  const { data, isLoading, isError } = useQuery<Channel[]>({
    queryKey: ['channels'],
    queryFn: () => authApi.get('/channels').then((r: any) => r.data ?? r),
    retry: false,
  });
  const channels = isError ? MOCK : data ?? [];

  const addChannel = useMutation({
    mutationFn: () => authApi.post('/channels', form),
    onSuccess: () => {
      setForm({ title: '', username: '' });
      setAdding(false);
      toast('Канал подключён', 'success');
      qc.invalidateQueries({ queryKey: ['channels'] });
    },
    onError: () => toast('Не удалось подключить канал', 'error'),
  });

  const fmt = (n: number) => n.toLocaleString('ru');

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-4 sm:p-6">
      <PageHeader
        title="Каналы"
        subtitle="Управление Telegram-каналами сети"
        action={
          <Button onClick={() => setAdding(true)}>
            <Plus size={16} /> Добавить
          </Button>
        }
      />

      {isLoading && !isError ? (
        <SkeletonList rows={4} />
      ) : channels.length === 0 ? (
        <EmptyState
          icon={<Radio size={26} />}
          title="Нет каналов"
          description="Подключите первый канал, где ваши аккаунты являются администраторами."
          action={
            <Button onClick={() => setAdding(true)}>
              <Plus size={16} /> Добавить канал
            </Button>
          }
        />
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          {channels.map((c) => (
            <Card key={c.id} className="p-4">
              <div className="flex items-center gap-3">
                <Avatar name={c.title} size={44} />
                <div className="min-w-0 flex-1">
                  <p className="truncate font-semibold text-fg">{c.title}</p>
                  <p className="truncate text-xs text-fg-hint">{c.username}</p>
                </div>
                <StatusBadge status={c.status} />
              </div>
              <div className="mt-4 grid grid-cols-3 gap-2 text-center">
                <div className="rounded-xl bg-surface-2 py-2.5">
                  <Users2 size={14} className="mx-auto text-fg-hint" />
                  <p className="mt-1 text-sm font-bold tabular-nums text-fg">{fmt(c.subscribers)}</p>
                  <p className="text-2xs text-fg-hint">подписчиков</p>
                </div>
                <div className="rounded-xl bg-surface-2 py-2.5">
                  <Eye size={14} className="mx-auto text-fg-hint" />
                  <p className="mt-1 text-sm font-bold tabular-nums text-fg">{fmt(c.dailyViews)}</p>
                  <p className="text-2xs text-fg-hint">просм./день</p>
                </div>
                <div className="rounded-xl bg-surface-2 py-2.5">
                  <TrendingUp size={14} className="mx-auto text-fg-hint" />
                  <p
                    className="mt-1 text-sm font-bold tabular-nums"
                    style={{ color: c.growth7d >= 0 ? 'var(--success)' : 'var(--danger)' }}
                  >
                    {c.growth7d >= 0 ? '+' : ''}
                    {c.growth7d}%
                  </p>
                  <p className="text-2xs text-fg-hint">за 7 дней</p>
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}

      <Sheet
        open={adding}
        onClose={() => setAdding(false)}
        title="Подключить канал"
        description="Аккаунт должен быть администратором канала"
        footer={
          <>
            <Button variant="secondary" onClick={() => setAdding(false)}>
              Отмена
            </Button>
            <Button onClick={() => addChannel.mutate()} disabled={!form.username} loading={addChannel.isPending}>
              Подключить
            </Button>
          </>
        }
      >
        <div className="space-y-3 py-1">
          <div>
            <Label>Название</Label>
            <Input value={form.title} onChange={(e) => setForm((f) => ({ ...f, title: e.target.value }))} placeholder="Новости сети" />
          </div>
          <div>
            <Label>Username</Label>
            <Input value={form.username} onChange={(e) => setForm((f) => ({ ...f, username: e.target.value }))} placeholder="@my_channel" />
          </div>
        </div>
      </Sheet>
    </div>
  );
}
