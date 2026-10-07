'use client';
import { useQuery } from '@tanstack/react-query';
import { authApi } from '@/lib/api';
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts';
import { Users, MessageSquare, MessagesSquare, Inbox } from 'lucide-react';
import { PageHeader, Card, Stat, Skeleton, EmptyState } from '@/components/ui';

export default function AnalyticsPage() {
  const { data, isLoading } = useQuery({
    queryKey: ['analytics-dashboard'],
    queryFn: () => authApi.get('/analytics/dashboard'),
    refetchInterval: 30_000,
  });

  const daily = data?.dailyMessages ?? [];

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-4 sm:p-6">
      <PageHeader title="Аналитика" subtitle="Активность пользователей и сообщений" />

      {isLoading ? (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-24" />
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <Stat label="Пользователей" value={(data?.totalUsers ?? 0).toLocaleString('ru')} icon={<Users size={17} />} tone="accent" />
          <Stat label="Открытых диалогов" value={(data?.openConversations ?? 0).toLocaleString('ru')} icon={<Inbox size={17} />} tone="success" />
          <Stat label="Активных диалогов" value={(data?.activeConversations ?? 0).toLocaleString('ru')} icon={<MessagesSquare size={17} />} tone="violet" />
          <Stat label="Сообщений" value={(data?.totalMessages ?? 0).toLocaleString('ru')} icon={<MessageSquare size={17} />} tone="warning" />
        </div>
      )}

      <Card className="p-4">
        <h2 className="mb-4 text-sm font-semibold text-fg">Сообщения за 7 дней</h2>
        {daily.length > 0 ? (
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={daily} margin={{ top: 4, right: 8, bottom: 4, left: -18 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
              <XAxis dataKey="date" tick={{ fontSize: 11, fill: 'var(--fg-hint)' }} axisLine={false} tickLine={false} />
              <YAxis tick={{ fontSize: 11, fill: 'var(--fg-hint)' }} axisLine={false} tickLine={false} />
              <Tooltip
                cursor={{ fill: 'var(--surface-2)' }}
                contentStyle={{
                  background: 'var(--surface)',
                  border: '1px solid var(--border)',
                  borderRadius: 12,
                  fontSize: 12,
                  color: 'var(--fg)',
                }}
              />
              <Bar dataKey="count" fill="var(--accent)" radius={[6, 6, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        ) : (
          <EmptyState
            icon={<MessageSquare size={24} />}
            title="Пока нет данных"
            description="Подключите ClickHouse для расширенной аналитики."
            className="border-0"
          />
        )}
      </Card>
    </div>
  );
}
