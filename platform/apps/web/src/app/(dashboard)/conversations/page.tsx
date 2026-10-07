'use client';
import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { authApi } from '@/lib/api';
import { MessageSquare, Users, Clock } from 'lucide-react';
import { PageHeader, Card, Stat, Badge, Avatar, Segmented, EmptyState } from '@/components/ui';

interface ConversationItem {
  id: string;
  userName: string;
  botName: string;
  lastMessage: string;
  updatedAt: string;
  status: 'open' | 'closed';
}

const MOCK: ConversationItem[] = [
  { id: '1', userName: '@alex_user', botName: '@MyBot', lastMessage: 'Спасибо за ответ!', updatedAt: new Date().toISOString(), status: 'open' },
  { id: '2', userName: 'Иван Петров', botName: '@ShopBot', lastMessage: 'Где мой заказ?', updatedAt: new Date(Date.now() - 3.6e6).toISOString(), status: 'open' },
  { id: '3', userName: '@maria99', botName: '@SupportBot', lastMessage: 'Всё решено, спасибо', updatedAt: new Date(Date.now() - 8.64e7).toISOString(), status: 'closed' },
];

function timeAgo(iso: string) {
  const min = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  if (min < 1) return 'только что';
  if (min < 60) return `${min} мин`;
  if (min < 1440) return `${Math.floor(min / 60)} ч`;
  return `${Math.floor(min / 1440)} дн`;
}

type Filter = 'all' | 'open' | 'closed';

export default function ConversationsPage() {
  const [filter, setFilter] = useState<Filter>('all');
  const { data, isError } = useQuery<ConversationItem[]>({
    queryKey: ['conversations'],
    queryFn: () => authApi.get('/conversations'),
  });
  const conversations = isError ? MOCK : data ?? MOCK;

  const last24h = useMemo(
    () => conversations.filter((c) => Date.now() - new Date(c.updatedAt).getTime() < 8.64e7).length,
    [conversations],
  );
  const open = conversations.filter((c) => c.status === 'open').length;
  const rows = conversations.filter((c) => filter === 'all' || c.status === filter);

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-4 sm:p-6">
      <PageHeader title="Разговоры" subtitle="Диалоги пользователей с ботами" />

      <div className="grid grid-cols-3 gap-3">
        <Stat label="Всего" value={conversations.length} icon={<MessageSquare size={17} />} tone="accent" />
        <Stat label="Открытых" value={open} icon={<Users size={17} />} tone="success" />
        <Stat label="За 24 ч" value={last24h} icon={<Clock size={17} />} tone="violet" />
      </div>

      <Segmented
        value={filter}
        onChange={setFilter}
        options={[
          { value: 'all', label: 'Все', count: conversations.length },
          { value: 'open', label: 'Открытые', count: open },
          { value: 'closed', label: 'Закрытые', count: conversations.length - open },
        ]}
      />

      {rows.length === 0 ? (
        <EmptyState icon={<MessageSquare size={26} />} title="Нет разговоров" description="По выбранному фильтру ничего нет." />
      ) : (
        <div className="space-y-2.5">
          {rows.map((c) => (
            <Card key={c.id} interactive className="flex items-center gap-3 p-3.5">
              <Avatar name={c.userName} size={42} />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <p className="truncate text-sm font-semibold text-fg">{c.userName}</p>
                  <span className="text-2xs text-fg-hint">· {c.botName}</span>
                </div>
                <p className="truncate text-xs text-fg-muted">{c.lastMessage}</p>
              </div>
              <div className="flex flex-col items-end gap-1">
                <Badge tone={c.status === 'open' ? 'success' : 'neutral'} dot>
                  {c.status === 'open' ? 'Открыт' : 'Закрыт'}
                </Badge>
                <span className="text-2xs text-fg-hint">{timeAgo(c.updatedAt)}</span>
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
