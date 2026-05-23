'use client';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { authApi } from '@/lib/api';
import { Bot, Plus, Trash2, BarChart2, X } from 'lucide-react';

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
  { id: 'mock-1', username: 'my_sales_bot', firstName: 'SalesBot', telegramId: '123456789', isActive: true, webhookSet: true, createdAt: new Date().toISOString(), _count: { conversations: 42 } },
  { id: 'mock-2', username: 'support_bot', firstName: 'SupportBot', telegramId: '987654321', isActive: false, webhookSet: false, createdAt: new Date().toISOString(), _count: { conversations: 7 } },
];

export default function BotsPage() {
  const qc = useQueryClient();
  const router = useRouter();

  const [name, setName] = useState('');
  const [token, setToken] = useState('');
  const [adding, setAdding] = useState(false);
  const [deleteConfirm, setDeleteConfirm] = useState<BotItem | null>(null);

  const { data: bots, isError } = useQuery<BotItem[]>({
    queryKey: ['bots'],
    queryFn: () => authApi.get('/bots'),
  });

  const displayBots: BotItem[] = isError ? MOCK_BOTS : (bots ?? []);

  const addBot = useMutation({
    mutationFn: () => authApi.post('/bots', { name, token }),
    onSuccess: () => {
      setName('');
      setToken('');
      setAdding(false);
      qc.invalidateQueries({ queryKey: ['bots'] });
    },
  });

  const deleteBot = useMutation({
    mutationFn: (id: string) => authApi.delete(`/bots/${id}`),
    onSuccess: () => {
      setDeleteConfirm(null);
      qc.invalidateQueries({ queryKey: ['bots'] });
    },
  });

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-slate-800">Боты</h1>
          <p className="text-sm text-slate-400 mt-0.5">Управление Telegram ботами платформы</p>
        </div>
        <button
          onClick={() => setAdding(true)}
          className="flex items-center gap-2 px-4 py-2 bg-sky-500 text-white text-sm font-medium rounded-lg hover:bg-sky-600 transition-colors"
        >
          <Plus size={15} /> Добавить бота
        </button>
      </div>

      {/* Add Bot Form */}
      {adding && (
        <div className="bg-white border border-slate-200 rounded-xl p-5 space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="font-semibold text-slate-700 text-sm">Новый бот</h2>
            <button onClick={() => setAdding(false)} className="text-slate-400 hover:text-slate-600">
              <X size={16} />
            </button>
          </div>
          <div className="flex gap-3">
            <input
              value={name}
              onChange={e => setName(e.target.value)}
              placeholder="Название бота (например: SalesBot)"
              className="flex-1 px-4 py-2 border border-slate-200 rounded-lg text-sm outline-none focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
            />
            <input
              value={token}
              onChange={e => setToken(e.target.value)}
              placeholder="Токен от @BotFather"
              className="flex-1 px-4 py-2 border border-slate-200 rounded-lg text-sm outline-none focus:border-sky-500 focus:ring-1 focus:ring-sky-500"
            />
            <button
              onClick={() => addBot.mutate()}
              disabled={!name || !token || addBot.isPending}
              className="px-4 py-2 bg-sky-500 text-white text-sm font-medium rounded-lg disabled:opacity-40 hover:bg-sky-600 transition-colors"
            >
              {addBot.isPending ? 'Добавление...' : 'Добавить'}
            </button>
          </div>
          {addBot.isError && (
            <p className="text-red-500 text-sm">Ошибка: неверный токен или бот недоступен</p>
          )}
        </div>
      )}

      {/* Mock data notice */}
      {isError && (
        <div className="bg-orange-50 border border-orange-200 rounded-lg px-4 py-2 text-sm text-orange-600">
          API недоступен — показаны демо-данные
        </div>
      )}

      {/* Bots list */}
      <div className="grid gap-4">
        {displayBots.map((b) => (
          <div key={b.id} className="bg-white border border-slate-200 rounded-xl p-5 flex items-center gap-4">
            <div className="w-10 h-10 bg-sky-100 rounded-full flex items-center justify-center shrink-0">
              <Bot size={18} className="text-sky-600" />
            </div>
            <div className="flex-1 min-w-0">
              <p className="font-medium text-slate-800">@{b.username ?? b.firstName}</p>
              <p className="text-xs text-slate-400 mt-0.5">
                ID: {b.telegramId} · {b._count?.conversations ?? 0} диалогов
              </p>
              <div className="flex gap-2 mt-1.5">
                <span
                  className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                    b.isActive ? 'bg-green-100 text-green-700' : 'bg-slate-100 text-slate-500'
                  }`}
                >
                  {b.isActive ? 'Активен' : 'Неактивен'}
                </span>
                <span
                  className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                    b.webhookSet ? 'bg-blue-100 text-blue-700' : 'bg-orange-100 text-orange-600'
                  }`}
                >
                  {b.webhookSet ? 'Webhook ✓' : 'Без webhook'}
                </span>
              </div>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <button
                onClick={() => router.push(`/bots/${b.id}`)}
                className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-sky-600 font-medium bg-sky-50 rounded-lg hover:bg-sky-100 transition-colors"
              >
                <BarChart2 size={14} /> Статистика
              </button>
              <button
                onClick={() => setDeleteConfirm(b)}
                className="p-2 text-slate-400 hover:text-red-500 rounded-lg hover:bg-red-50 transition-colors"
                title="Удалить бота"
              >
                <Trash2 size={16} />
              </button>
            </div>
          </div>
        ))}

        {displayBots.length === 0 && (
          <div className="text-center py-12 text-slate-400">
            <Bot size={36} className="mx-auto mb-3 opacity-30" />
            <p>Нет ботов. Добавьте первый бот по токену.</p>
          </div>
        )}
      </div>

      {/* Delete Confirmation Modal */}
      {deleteConfirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="bg-white rounded-2xl shadow-xl p-6 w-full max-w-sm mx-4 space-y-4">
            <div className="flex items-start justify-between">
              <h2 className="font-semibold text-slate-800 text-base">Удалить бота?</h2>
              <button onClick={() => setDeleteConfirm(null)} className="text-slate-400 hover:text-slate-600">
                <X size={18} />
              </button>
            </div>
            <p className="text-sm text-slate-500">
              Вы уверены, что хотите удалить бота{' '}
              <span className="font-medium text-slate-700">
                @{deleteConfirm.username ?? deleteConfirm.firstName}
              </span>
              ? Это действие необратимо.
            </p>
            <div className="flex gap-3 justify-end">
              <button
                onClick={() => setDeleteConfirm(null)}
                className="px-4 py-2 text-sm text-slate-600 border border-slate-200 rounded-lg hover:bg-slate-50 transition-colors"
              >
                Отмена
              </button>
              <button
                onClick={() => deleteBot.mutate(deleteConfirm.id)}
                disabled={deleteBot.isPending}
                className="px-4 py-2 text-sm text-white bg-red-500 rounded-lg hover:bg-red-600 disabled:opacity-40 transition-colors"
              >
                {deleteBot.isPending ? 'Удаление...' : 'Удалить'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
