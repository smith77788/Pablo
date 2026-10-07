'use client';
import { useQuery } from '@tanstack/react-query';
import { useParams } from 'next/navigation';
import { authApi } from '@/lib/api';
import { Users, MessageSquare } from 'lucide-react';
import { PageHeader, Card, Stat, Badge, Avatar, Skeleton } from '@/components/ui';

interface BotStats {
  userCount: number;
  messageCount: number;
}
interface BotInfo {
  id: string;
  username?: string;
  firstName?: string;
  telegramId?: string;
  isActive: boolean;
  webhookSet: boolean;
}

const MOCK_STATS: BotStats = { userCount: 128, messageCount: 3452 };
const MOCK_BOT: BotInfo = { id: 'm1', username: 'sales_bot', firstName: 'SalesBot', telegramId: '123456789', isActive: true, webhookSet: true };

export default function BotStatsPage() {
  const { id } = useParams<{ id: string }>();

  const { data: bot, isError: botError } = useQuery<BotInfo>({
    queryKey: ['bot', id],
    queryFn: () => authApi.get(`/bots/${id}`),
    enabled: !!id,
  });
  const { data: stats, isLoading, isError: statsError } = useQuery<BotStats>({
    queryKey: ['bot-stats', id],
    queryFn: () => authApi.get(`/bots/${id}/stats`),
    enabled: !!id,
    refetchInterval: 30_000,
  });

  const b = botError ? MOCK_BOT : bot ?? MOCK_BOT;
  const s = statsError ? MOCK_STATS : stats ?? { userCount: 0, messageCount: 0 };

  return (
    <div className="mx-auto max-w-3xl space-y-5 p-4 sm:p-6">
      <PageHeader title={`@${b.username ?? b.firstName}`} subtitle={`Telegram ID ${b.telegramId}`} />

      <Card className="flex items-center gap-4 p-4">
        <Avatar name={b.username ?? b.firstName ?? 'bot'} size={52} />
        <div className="flex-1">
          <p className="font-semibold text-fg">@{b.username ?? b.firstName}</p>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            <Badge tone={b.isActive ? 'success' : 'neutral'} dot>
              {b.isActive ? 'Активен' : 'Неактивен'}
            </Badge>
            <Badge tone={b.webhookSet ? 'accent' : 'warning'}>{b.webhookSet ? 'Webhook ✓' : 'Без webhook'}</Badge>
            {(botError || statsError) && <Badge tone="warning">демо-данные</Badge>}
          </div>
        </div>
      </Card>

      {isLoading ? (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Skeleton className="h-24" />
          <Skeleton className="h-24" />
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Stat label="Всего пользователей" value={s.userCount.toLocaleString('ru')} icon={<Users size={17} />} tone="accent" />
          <Stat label="Сообщений обработано" value={s.messageCount.toLocaleString('ru')} icon={<MessageSquare size={17} />} tone="violet" />
        </div>
      )}
    </div>
  );
}
