'use client';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { Bot, Plus, Trash2, BarChart2 } from 'lucide-react';
import { authApi } from '@/lib/api';
import {
  PageHeader,
  Button,
  Card,
  Badge,
  Avatar,
  Sheet,
  Input,
  Label,
  EmptyState,
  SkeletonList,
  useToast,
} from '@/components/ui';

interface BotItem {
  id: string;
  username?: string;
  firstName?: string;
  telegramId?: string;
  isActive: boolean;
  webhookSet: boolean;
  createdAt: string;
  _count?: { conversations: number };
}

const MOCK_BOTS: BotItem[] = [
  { id: 'm1', username: 'sales_bot', firstName: 'SalesBot', telegramId: '123456789', isActive: true, webhookSet: true, createdAt: '', _count: { conversations: 42 } },
  { id: 'm2', username: 'support_bot', firstName: 'SupportBot', telegramId: '987654321', isActive: false, webhookSet: false, createdAt: '', _count: { conversations: 7 } },
];

export default function BotsPage() {
  const qc = useQueryClient();
  const router = useRouter();
  const toast = useToast();

  const [name, setName] = useState('');
  const [token, setToken] = useState('');
  const [adding, setAdding] = useState(false);
  const [deleteConfirm, setDeleteConfirm] = useState<BotItem | null>(null);

  const { data: bots, isError, isLoading } = useQuery<BotItem[]>({
    queryKey: ['bots'],
    queryFn: () => authApi.get('/bots'),
    retry: false,
  });
  const displayBots: BotItem[] = isError ? MOCK_BOTS : bots ?? [];

  const addBot = useMutation({
    mutationFn: () => authApi.post('/bots', { name, token }),
    onSuccess: () => {
      setName('');
      setToken('');
      setAdding(false);
      toast('Бот добавлен', 'success');
      qc.invalidateQueries({ queryKey: ['bots'] });
    },
    onError: () => toast('Неверный токен или бот недоступен', 'error'),
  });

  const deleteBot = useMutation({
    mutationFn: (id: string) => authApi.delete(`/bots/${id}`),
    onSuccess: () => {
      setDeleteConfirm(null);
      toast('Бот удалён', 'success');
      qc.invalidateQueries({ queryKey: ['bots'] });
    },
    onError: () => toast('Не удалось удалить бота', 'error'),
  });

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-4 sm:p-6">
      <PageHeader
        title="Боты"
        subtitle="Telegram-боты вашей сети"
        action={
          <Button onClick={() => setAdding(true)}>
            <Plus size={16} /> Добавить
          </Button>
        }
      />

      {isError && (
        <div className="rounded-xl bg-warning-weak px-4 py-2.5 text-sm font-medium text-warning">
          API недоступен — показаны демо-данные
        </div>
      )}

      {isLoading && !isError ? (
        <SkeletonList rows={3} />
      ) : displayBots.length === 0 ? (
        <EmptyState
          icon={<Bot size={26} />}
          title="Пока нет ботов"
          description="Добавьте первого бота по токену от @BotFather."
          action={
            <Button onClick={() => setAdding(true)}>
              <Plus size={16} /> Добавить бота
            </Button>
          }
        />
      ) : (
        <div className="grid gap-3 sm:grid-cols-2">
          {displayBots.map((b) => (
            <Card key={b.id} className="flex items-center gap-3 p-4">
              <Avatar name={b.username ?? b.firstName ?? 'bot'} size={44} />
              <div className="min-w-0 flex-1">
                <p className="truncate font-semibold text-fg">@{b.username ?? b.firstName}</p>
                <p className="mt-0.5 text-xs text-fg-hint">
                  ID {b.telegramId} · {b._count?.conversations ?? 0} диалогов
                </p>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  <Badge tone={b.isActive ? 'success' : 'neutral'} dot>
                    {b.isActive ? 'Активен' : 'Неактивен'}
                  </Badge>
                  <Badge tone={b.webhookSet ? 'accent' : 'warning'}>
                    {b.webhookSet ? 'Webhook ✓' : 'Без webhook'}
                  </Badge>
                </div>
              </div>
              <div className="flex flex-col gap-2">
                <Button size="icon" variant="secondary" onClick={() => router.push(`/bots/${b.id}`)} title="Статистика">
                  <BarChart2 size={16} />
                </Button>
                <Button size="icon" variant="ghost" onClick={() => setDeleteConfirm(b)} title="Удалить">
                  <Trash2 size={16} />
                </Button>
              </div>
            </Card>
          ))}
        </div>
      )}

      {/* Add bot sheet */}
      <Sheet
        open={adding}
        onClose={() => setAdding(false)}
        title="Новый бот"
        description="Подключите бота по токену из @BotFather"
        footer={
          <>
            <Button variant="secondary" onClick={() => setAdding(false)}>
              Отмена
            </Button>
            <Button onClick={() => addBot.mutate()} disabled={!name || !token} loading={addBot.isPending}>
              Добавить
            </Button>
          </>
        }
      >
        <div className="space-y-4 py-1">
          <div>
            <Label htmlFor="bn">Название</Label>
            <Input id="bn" value={name} onChange={(e) => setName(e.target.value)} placeholder="SalesBot" />
          </div>
          <div>
            <Label htmlFor="bt">Токен</Label>
            <Input id="bt" value={token} onChange={(e) => setToken(e.target.value)} placeholder="123456:AA..." />
          </div>
        </div>
      </Sheet>

      {/* Delete confirm */}
      <Sheet
        open={!!deleteConfirm}
        onClose={() => setDeleteConfirm(null)}
        size="sm"
        title="Удалить бота?"
        description={`@${deleteConfirm?.username ?? deleteConfirm?.firstName} будет отключён. Действие необратимо.`}
        footer={
          <>
            <Button variant="secondary" onClick={() => setDeleteConfirm(null)}>
              Отмена
            </Button>
            <Button variant="danger" onClick={() => deleteConfirm && deleteBot.mutate(deleteConfirm.id)} loading={deleteBot.isPending}>
              Удалить
            </Button>
          </>
        }
      />
    </div>
  );
}
