'use client';
import { useQuery } from '@tanstack/react-query';
import { useParams, useRouter } from 'next/navigation';
import { authApi } from '@/lib/api';
import { ArrowLeft, Bot, Users, MessageSquare, BarChart2 } from 'lucide-react';

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
const MOCK_BOT: BotInfo = { id: 'mock-1', username: 'my_sales_bot', firstName: 'SalesBot', telegramId: '123456789', isActive: true, webhookSet: true };

export default function BotStatsPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();

  const { data: bot, isError: botError } = useQuery<BotInfo>({
    queryKey: ['bot', id],
    queryFn: () => authApi.get(`/bots/${id}`),
    enabled: !!id,
  });

  const { data: stats, isLoading: statsLoading, isError: statsError } = useQuery<BotStats>({
    queryKey: ['bot-stats', id],
    queryFn: () => authApi.get(`/bots/${id}/stats`),
    enabled: !!id,
    refetchInterval: 30_000,
  });

  const displayBot: BotInfo = botError ? MOCK_BOT : (bot ?? MOCK_BOT);
  const displayStats: BotStats = statsError ? MOCK_STATS : (stats ?? { userCount: 0, messageCount: 0 });

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center gap-4">
        <button
          onClick={() => router.back()}
          className="p-2 text-slate-400 hover:text-slate-700 rounded-lg hover:bg-slate-100 transition-colors"
        >
          <ArrowLeft size={18} />
        </button>
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 bg-sky-100 rounded-full flex items-center justify-center">
            <Bot size={18} className="text-sky-600" />
          </div>
          <div>
            <h1 className="text-xl font-bold text-slate-800">
              @{displayBot.username ?? displayBot.firstName}
            </h1>
            <p className="text-sm text-slate-400 mt-0.5">
              Telegram ID: {displayBot.telegramId}
              {(botError || statsError) && (
                <span className="ml-2 text-orange-500">· демо-данные</span>
              )}
            </p>
          </div>
        </div>
        <div className="ml-auto flex gap-2">
          <span
            className={`text-xs px-2.5 py-1 rounded-full font-medium ${
              displayBot.isActive ? 'bg-green-100 text-green-700' : 'bg-slate-100 text-slate-500'
            }`}
          >
            {displayBot.isActive ? 'Активен' : 'Неактивен'}
          </span>
          <span
            className={`text-xs px-2.5 py-1 rounded-full font-medium ${
              displayBot.webhookSet ? 'bg-blue-100 text-blue-700' : 'bg-orange-100 text-orange-600'
            }`}
          >
            {displayBot.webhookSet ? 'Webhook ✓' : 'Без webhook'}
          </span>
        </div>
      </div>

      {/* Stats cards */}
      <div>
        <div className="flex items-center gap-2 mb-4">
          <BarChart2 size={16} className="text-slate-400" />
          <h2 className="font-semibold text-slate-700 text-sm">Статистика</h2>
        </div>

        {statsLoading ? (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            {[0, 1].map(i => (
              <div key={i} className="bg-white rounded-xl border border-slate-200 p-5 h-28 animate-pulse">
                <div className="h-4 bg-slate-100 rounded w-2/3 mb-4" />
                <div className="h-8 bg-slate-100 rounded w-1/2" />
              </div>
            ))}
          </div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div className="bg-white rounded-xl border border-slate-200 p-5 flex flex-col gap-3">
              <div className="flex items-center justify-between">
                <span className="text-sm text-slate-500 font-medium">Всего пользователей</span>
                <span className="w-9 h-9 rounded-lg flex items-center justify-center bg-sky-50 text-sky-600">
                  <Users size={17} />
                </span>
              </div>
              <p className="text-3xl font-bold text-slate-800">
                {displayStats.userCount.toLocaleString('ru')}
              </p>
            </div>

            <div className="bg-white rounded-xl border border-slate-200 p-5 flex flex-col gap-3">
              <div className="flex items-center justify-between">
                <span className="text-sm text-slate-500 font-medium">Сообщений обработано</span>
                <span className="w-9 h-9 rounded-lg flex items-center justify-center bg-violet-50 text-violet-600">
                  <MessageSquare size={17} />
                </span>
              </div>
              <p className="text-3xl font-bold text-slate-800">
                {displayStats.messageCount.toLocaleString('ru')}
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
