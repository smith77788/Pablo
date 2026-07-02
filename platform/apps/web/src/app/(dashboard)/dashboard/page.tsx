'use client';
import { useQuery } from '@tanstack/react-query';
import { authApi } from '@/lib/api';
import { Users, MessageSquare, GitBranch, MessageCircleReply, ArrowUp, Clock } from 'lucide-react';

interface StatsOverview {
  totalUsers: number;
  newToday: number;
  messagesSent: number;
  messagesReceived: number;
  activeFunnels: number;
  activeReplies: number;
}

interface MetricCardProps {
  label: string;
  value: number;
  subLabel?: string;
  subValue?: number;
  icon: React.ReactNode;
  accent?: string;
}

function MetricCard({ label, value, subLabel, subValue, icon, accent = 'bg-sky-50 text-sky-600' }: MetricCardProps) {
  return (
    <div className="bg-white rounded-xl border border-slate-200 p-5 flex flex-col gap-3">
      <div className="flex items-center justify-between">
        <span className="text-sm text-slate-500 font-medium">{label}</span>
        <span className={`w-9 h-9 rounded-lg flex items-center justify-center ${accent}`}>
          {icon}
        </span>
      </div>
      <div>
        <p className="text-3xl font-bold text-slate-800">{value.toLocaleString('ru')}</p>
        {subLabel && subValue !== undefined && (
          <p className="text-xs text-slate-400 mt-1 flex items-center gap-1">
            <ArrowUp size={11} className="text-green-500" />
            <span className="text-green-600 font-medium">+{subValue.toLocaleString('ru')}</span>
            &nbsp;{subLabel}
          </p>
        )}
      </div>
    </div>
  );
}

const MOCK_EVENTS = [
  { id: 1, type: 'Новый пользователь', detail: '@user_ivan подписался на бота', time: '2 мин назад' },
  { id: 2, type: 'Сообщение', detail: 'Входящее сообщение от @user_maria', time: '5 мин назад' },
  { id: 3, type: 'Цепочка запущена', detail: 'Funnel "Онбординг" — шаг 1', time: '12 мин назад' },
  { id: 4, type: 'Авто-ответ', detail: 'Сработало правило "Приветствие"', time: '18 мин назад' },
  { id: 5, type: 'Рассылка', detail: 'Broadcast "Акция мая" отправлен 142 пользователям', time: '34 мин назад' },
  { id: 6, type: 'Новый пользователь', detail: '@user_alexey подписался на бота', time: '51 мин назад' },
  { id: 7, type: 'Сообщение', detail: 'Входящее сообщение от @user_oksana', time: '1 ч назад' },
  { id: 8, type: 'Цепочка завершена', detail: 'Funnel "Регистрация" — финальный шаг', time: '1 ч назад' },
];

const EVENT_TYPE_STYLES: Record<string, string> = {
  'Новый пользователь': 'bg-green-100 text-green-700',
  'Сообщение': 'bg-sky-100 text-sky-700',
  'Цепочка запущена': 'bg-violet-100 text-violet-700',
  'Цепочка завершена': 'bg-violet-100 text-violet-700',
  'Авто-ответ': 'bg-orange-100 text-orange-700',
  'Рассылка': 'bg-pink-100 text-pink-700',
};

export default function DashboardPage() {
  const { data, isLoading } = useQuery<StatsOverview>({
    queryKey: ['stats-overview'],
    queryFn: () => authApi.get('/stats/overview'),
    refetchInterval: 30_000,
  });

  const stats: StatsOverview = data ?? {
    totalUsers: 0,
    newToday: 0,
    messagesSent: 0,
    messagesReceived: 0,
    activeFunnels: 0,
    activeReplies: 0,
  };

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-xl font-bold text-slate-800">Статистика</h1>
        <p className="text-sm text-slate-400 mt-0.5">Общий обзор активности платформы</p>
      </div>

      {/* Metric cards */}
      {isLoading ? (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="bg-white rounded-xl border border-slate-200 p-5 h-28 animate-pulse">
              <div className="h-4 bg-slate-100 rounded w-2/3 mb-4" />
              <div className="h-8 bg-slate-100 rounded w-1/2" />
            </div>
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-2 lg:grid-cols-3 gap-4">
          <MetricCard
            label="Всего пользователей"
            value={stats.totalUsers}
            subLabel="новых сегодня"
            subValue={stats.newToday}
            icon={<Users size={17} />}
            accent="bg-sky-50 text-sky-600"
          />
          <MetricCard
            label="Сообщений отправлено"
            value={stats.messagesSent}
            icon={<MessageSquare size={17} />}
            accent="bg-violet-50 text-violet-600"
          />
          <MetricCard
            label="Сообщений получено"
            value={stats.messagesReceived}
            icon={<MessageCircleReply size={17} />}
            accent="bg-emerald-50 text-emerald-600"
          />
          <MetricCard
            label="Активных цепочек"
            value={stats.activeFunnels}
            icon={<GitBranch size={17} />}
            accent="bg-orange-50 text-orange-600"
          />
          <MetricCard
            label="Активных авто-ответов"
            value={stats.activeReplies}
            icon={<MessageCircleReply size={17} />}
            accent="bg-pink-50 text-pink-600"
          />
        </div>
      )}

      {/* Recent events table */}
      <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
        <div className="px-5 py-4 border-b border-slate-100 flex items-center gap-2">
          <Clock size={15} className="text-slate-400" />
          <h2 className="font-semibold text-slate-700 text-sm">Последние события</h2>
          <span className="ml-auto text-xs text-slate-400">Mock-данные</span>
        </div>
        <div className="divide-y divide-slate-50">
          {MOCK_EVENTS.map((event) => (
            <div key={event.id} className="px-5 py-3 flex items-center gap-4 hover:bg-slate-50 transition-colors">
              <span
                className={`text-xs font-medium px-2 py-0.5 rounded-full whitespace-nowrap ${
                  EVENT_TYPE_STYLES[event.type] ?? 'bg-slate-100 text-slate-600'
                }`}
              >
                {event.type}
              </span>
              <span className="flex-1 text-sm text-slate-600 truncate">{event.detail}</span>
              <span className="text-xs text-slate-400 whitespace-nowrap">{event.time}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
