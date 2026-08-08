'use client';
import { useQuery } from '@tanstack/react-query';
import { authApi } from '@/lib/api';
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts';

function StatCard({ label, value, sub }: { label: string; value: number; sub?: string }) {
  return (
    <div className="bg-white rounded-xl border border-slate-200 p-5">
      <p className="text-sm text-slate-500 mb-1">{label}</p>
      <p className="text-3xl font-bold text-slate-800">{value.toLocaleString('ru')}</p>
      {sub && <p className="text-xs text-slate-400 mt-1">{sub}</p>}
    </div>
  );
}

export default function AnalyticsPage() {
  const { data, isLoading } = useQuery({
    queryKey: ['analytics-dashboard'],
    queryFn: () => authApi.get('/analytics/dashboard'),
    refetchInterval: 30_000,
  });

  if (isLoading) return <div className="flex items-center justify-center h-full text-slate-400">Загрузка...</div>;

  const daily = data?.dailyMessages ?? [];

  return (
    <div className="p-6 space-y-6">
      <h1 className="text-xl font-bold text-slate-800">Аналитика</h1>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard label="Всего пользователей" value={data?.totalUsers ?? 0} />
        <StatCard label="Открытых диалогов" value={data?.openConversations ?? 0} />
        <StatCard label="Активных диалогов" value={data?.activeConversations ?? 0} />
        <StatCard label="Сообщений получено" value={data?.totalMessages ?? 0} />
      </div>

      <div className="bg-white rounded-xl border border-slate-200 p-5">
        <h2 className="font-semibold text-slate-700 mb-4">Сообщения за 7 дней</h2>
        {daily.length > 0 ? (
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={daily} margin={{ top: 4, right: 8, bottom: 4, left: -16 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
              <XAxis dataKey="date" tick={{ fontSize: 11 }} />
              <YAxis tick={{ fontSize: 11 }} />
              <Tooltip />
              <Bar dataKey="count" fill="#0ea5e9" radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        ) : (
          <div className="h-48 flex items-center justify-center text-slate-400 text-sm">
            Нет данных. Подключите ClickHouse для расширенной аналитики.
          </div>
        )}
      </div>
    </div>
  );
}
