'use client';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import Link from 'next/link';
import { authApi } from '@/lib/api';
import { Send, Play, Plus } from 'lucide-react';
import { format } from 'date-fns';
import { ru } from 'date-fns/locale';
import { PageHeader, Button, Card, Stat, Badge, EmptyState, useToast, type Tone } from '@/components/ui';

interface BroadcastItem {
  id: string;
  botName: string;
  status: string;
  total: number;
  sent: number;
  failed: number;
  createdAt: string;
  preview: string;
}

const MOCK: BroadcastItem[] = [
  { id: '1', botName: '@MyBot', status: 'done', total: 1500, sent: 1480, failed: 20, createdAt: new Date().toISOString(), preview: 'Привет! Специальное предложение…' },
  { id: '2', botName: '@ShopBot', status: 'running', total: 800, sent: 450, failed: 5, createdAt: new Date(Date.now() - 3.6e6).toISOString(), preview: 'Скидка 50% только сегодня!' },
  { id: '3', botName: '@MyBot', status: 'pending', total: 2000, sent: 0, failed: 0, createdAt: new Date(Date.now() - 8.64e7).toISOString(), preview: 'Новое обновление приложения…' },
];

const STATUS: Record<string, { tone: Tone; label: string }> = {
  done: { tone: 'success', label: 'Завершена' },
  COMPLETED: { tone: 'success', label: 'Завершена' },
  running: { tone: 'accent', label: 'Идёт' },
  RUNNING: { tone: 'accent', label: 'Идёт' },
  pending: { tone: 'warning', label: 'Ожидает' },
  DRAFT: { tone: 'neutral', label: 'Черновик' },
  SCHEDULED: { tone: 'accent', label: 'Запланирована' },
  CANCELLED: { tone: 'neutral', label: 'Отменена' },
  PAUSED: { tone: 'warning', label: 'Пауза' },
};

const normalize = (bc: any): BroadcastItem => ({
  id: bc.id,
  botName: bc.botName ?? (bc.bot ? `@${bc.bot.username ?? bc.bot.firstName}` : '—'),
  status: bc.status,
  total: bc.total ?? bc.totalCount ?? 0,
  sent: bc.sent ?? bc.sentCount ?? 0,
  failed: bc.failed ?? bc.failedCount ?? 0,
  createdAt: bc.createdAt,
  preview: bc.preview ?? (typeof bc.message === 'object' ? bc.message?.text ?? '' : bc.message ?? ''),
});

const isDone = (s: string) => s === 'done' || s === 'COMPLETED';
const canLaunch = (s: string) => s === 'DRAFT' || s === 'SCHEDULED' || s === 'pending';

export default function BroadcastsPage() {
  const qc = useQueryClient();
  const toast = useToast();

  const { data: raw, isError } = useQuery({
    queryKey: ['broadcasts'],
    queryFn: () => authApi.get('/broadcasts'),
  });

  const launch = useMutation({
    mutationFn: (id: string) => authApi.post(`/broadcasts/${id}/launch`),
    onSuccess: () => {
      toast('Рассылка запущена', 'success');
      qc.invalidateQueries({ queryKey: ['broadcasts'] });
    },
    onError: () => toast('Не удалось запустить рассылку', 'error'),
  });

  const apiBcs: BroadcastItem[] = Array.isArray(raw) ? raw.map(normalize) : [];
  const usingMock = isError || apiBcs.length === 0;
  const broadcasts = usingMock ? MOCK : apiBcs;

  const reached = broadcasts.filter((b) => isDone(b.status)).reduce((s, b) => s + b.sent, 0);

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-4 sm:p-6">
      <PageHeader
        title="Рассылки"
        subtitle="Кампании по вашей базе подписчиков"
        action={
          <Link href="/broadcasts/new">
            <Button>
              <Plus size={16} /> Новая
            </Button>
          </Link>
        }
      />

      <div className="grid grid-cols-3 gap-3">
        <Stat label="Всего" value={broadcasts.length} icon={<Send size={17} />} tone="accent" />
        <Stat label="Завершено" value={broadcasts.filter((b) => isDone(b.status)).length} tone="success" />
        <Stat label="Охвачено" value={reached.toLocaleString('ru')} tone="violet" />
      </div>

      {broadcasts.length === 0 ? (
        <EmptyState icon={<Send size={26} />} title="Рассылок пока нет" description="Создайте первую кампанию по своей базе." />
      ) : (
        <div className="space-y-2.5">
          {broadcasts.map((bc) => {
            const pct = bc.total > 0 ? Math.round((bc.sent / bc.total) * 100) : 0;
            const st = STATUS[bc.status] ?? { tone: 'neutral' as Tone, label: bc.status };
            return (
              <Card key={bc.id} className="p-4">
                <div className="flex items-center gap-3">
                  <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-accent-weak text-accent">
                    <Send size={16} />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <p className="truncate text-sm font-semibold text-fg">{bc.botName}</p>
                      <Badge tone={st.tone} dot>
                        {st.label}
                      </Badge>
                    </div>
                    <p className="truncate text-xs text-fg-hint">
                      {bc.preview} · {format(new Date(bc.createdAt), 'd MMM HH:mm', { locale: ru })}
                    </p>
                  </div>
                  {canLaunch(bc.status) && !usingMock && (
                    <Button size="sm" variant="success" onClick={() => launch.mutate(bc.id)} loading={launch.isPending}>
                      <Play size={13} /> Запустить
                    </Button>
                  )}
                </div>
                <div className="mt-3">
                  <div className="mb-1 flex justify-between text-2xs text-fg-hint">
                    <span>
                      {bc.sent.toLocaleString('ru')} / {bc.total.toLocaleString('ru')}
                      {bc.failed > 0 && ` · ${bc.failed} ошибок`}
                    </span>
                    <span>{pct}%</span>
                  </div>
                  <div className="h-1.5 overflow-hidden rounded-full bg-surface-2">
                    <div
                      className="h-full rounded-full transition-all"
                      style={{ width: `${pct}%`, background: isDone(bc.status) ? 'var(--success)' : 'var(--accent)' }}
                    />
                  </div>
                </div>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}
