'use client';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { authApi } from '@/lib/api';
import { MessageSquare, Users, Clock } from 'lucide-react';

interface ConversationItem {
  id: string;
  userName: string;
  botName: string;
  lastMessage: string;
  updatedAt: string;
  status: 'open' | 'closed';
}

const MOCK_CONVERSATIONS: ConversationItem[] = [
  {
    id: '1',
    userName: '@alex_user',
    botName: '@MyBot',
    lastMessage: 'Спасибо за ответ!',
    updatedAt: new Date().toISOString(),
    status: 'open',
  },
  {
    id: '2',
    userName: 'Иван Петров',
    botName: '@ShopBot',
    lastMessage: 'Где мой заказ?',
    updatedAt: new Date(Date.now() - 3600000).toISOString(),
    status: 'open',
  },
  {
    id: '3',
    userName: '@maria99',
    botName: '@SupportBot',
    lastMessage: 'Всё решено, спасибо',
    updatedAt: new Date(Date.now() - 86400000).toISOString(),
    status: 'closed',
  },
];

type StatusFilter = 'all' | 'open' | 'closed';

function formatTime(iso: string): string {
  const date = new Date(iso);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMin = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMs / 3600000);
  const diffDays = Math.floor(diffMs / 86400000);

  if (diffMin < 1) return 'только что';
  if (diffMin < 60) return `${diffMin} мин назад`;
  if (diffHours < 24) return `${diffHours} ч назад`;
  return `${diffDays} дн назад`;
}

function truncate(text: string, max = 60): string {
  return text.length > max ? text.slice(0, max) + '…' : text;
}

function countLast24h(conversations: ConversationItem[]): number {
  const cutoff = Date.now() - 86400000;
  return conversations.filter((c) => new Date(c.updatedAt).getTime() >= cutoff).length;
}

export default function ConversationsPage() {
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');

  const { data, isError } = useQuery<ConversationItem[]>({
    queryKey: ['conversations'],
    queryFn: () => authApi.get('/conversations'),
  });

  const conversations: ConversationItem[] = isError ? MOCK_CONVERSATIONS : (data ?? MOCK_CONVERSATIONS);

  const totalCount = conversations.length;
  const openCount = conversations.filter((c) => c.status === 'open').length;
  const last24hCount = countLast24h(conversations);

  const filtered = conversations.filter((c) => {
    if (statusFilter === 'open') return c.status === 'open';
    if (statusFilter === 'closed') return c.status === 'closed';
    return true;
  });

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-xl font-bold text-slate-800">💬 Входящие сообщения</h1>
        <p className="text-sm text-slate-400 mt-0.5">Разговоры пользователей с ботами</p>
      </div>

      {/* Mock data notice */}
      {isError && (
        <div className="bg-orange-50 border border-orange-200 rounded-lg px-4 py-2 text-sm text-orange-600">
          API недоступен — показаны демо-данные
        </div>
      )}

      {/* Stats cards */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <div className="bg-white rounded-xl border border-slate-200 p-5 flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <span className="text-sm text-slate-500 font-medium">Всего разговоров</span>
            <span className="w-9 h-9 rounded-lg flex items-center justify-center bg-sky-50 text-sky-600">
              <MessageSquare size={17} />
            </span>
          </div>
          <p className="text-3xl font-bold text-slate-800">{totalCount}</p>
        </div>

        <div className="bg-white rounded-xl border border-slate-200 p-5 flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <span className="text-sm text-slate-500 font-medium">Открытых</span>
            <span className="w-9 h-9 rounded-lg flex items-center justify-center bg-green-50 text-green-600">
              <Users size={17} />
            </span>
          </div>
          <p className="text-3xl font-bold text-slate-800">{openCount}</p>
        </div>

        <div className="bg-white rounded-xl border border-slate-200 p-5 flex flex-col gap-3">
          <div className="flex items-center justify-between">
            <span className="text-sm text-slate-500 font-medium">За последние 24ч</span>
            <span className="w-9 h-9 rounded-lg flex items-center justify-center bg-violet-50 text-violet-600">
              <Clock size={17} />
            </span>
          </div>
          <p className="text-3xl font-bold text-slate-800">{last24hCount}</p>
        </div>
      </div>

      {/* Filter buttons */}
      <div className="flex gap-2">
        {(['all', 'open', 'closed'] as StatusFilter[]).map((f) => (
          <button
            key={f}
            onClick={() => setStatusFilter(f)}
            className={`px-4 py-1.5 rounded-lg text-sm font-medium transition-colors ${
              statusFilter === f
                ? 'bg-sky-500 text-white'
                : 'bg-white border border-slate-200 text-slate-600 hover:bg-slate-50'
            }`}
          >
            {f === 'all' ? 'Все' : f === 'open' ? 'Открытые' : 'Закрытые'}
          </button>
        ))}
      </div>

      {/* Conversations table */}
      <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
        {/* Table header */}
        <div className="grid grid-cols-[1fr_1fr_2fr_auto_auto] gap-4 px-5 py-3 border-b border-slate-100 text-xs font-medium text-slate-400 uppercase tracking-wide">
          <span>Пользователь</span>
          <span>Бот</span>
          <span>Последнее сообщение</span>
          <span>Время</span>
          <span>Статус</span>
        </div>

        {/* Table rows */}
        <div className="divide-y divide-slate-50">
          {filtered.map((conv) => (
            <div
              key={conv.id}
              className="grid grid-cols-[1fr_1fr_2fr_auto_auto] gap-4 px-5 py-3.5 items-center hover:bg-slate-50 transition-colors cursor-pointer"
            >
              <span className="text-sm font-medium text-slate-700 truncate">{conv.userName}</span>
              <span className="text-sm text-slate-500 truncate">{conv.botName}</span>
              <span className="text-sm text-slate-600 truncate">{truncate(conv.lastMessage)}</span>
              <span className="text-xs text-slate-400 whitespace-nowrap">{formatTime(conv.updatedAt)}</span>
              <span
                className={`text-xs px-2 py-0.5 rounded-full font-medium whitespace-nowrap ${
                  conv.status === 'open'
                    ? 'bg-green-100 text-green-700'
                    : 'bg-slate-100 text-slate-500'
                }`}
              >
                {conv.status === 'open' ? 'Открыт' : 'Закрыт'}
              </span>
            </div>
          ))}

          {filtered.length === 0 && (
            <div className="text-center py-12 text-slate-400">
              <MessageSquare size={36} className="mx-auto mb-3 opacity-30" />
              <p>Нет разговоров по выбранному фильтру</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
