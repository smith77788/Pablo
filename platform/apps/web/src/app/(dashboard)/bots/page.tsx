'use client';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { authApi } from '@/lib/api';
import { Bot, Plus, Trash2 } from 'lucide-react';

export default function BotsPage() {
  const qc = useQueryClient();
  const [token, setToken] = useState('');
  const [adding, setAdding] = useState(false);

  const { data: bots } = useQuery({ queryKey: ['bots'], queryFn: () => authApi.get('/bots') });

  const addBot = useMutation({
    mutationFn: () => authApi.post('/bots', { token }),
    onSuccess: () => { setToken(''); setAdding(false); qc.invalidateQueries({ queryKey: ['bots'] }); },
  });

  const deleteBot = useMutation({
    mutationFn: (id: string) => authApi.delete(`/bots/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['bots'] }),
  });

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold text-slate-800">Боты</h1>
        <button onClick={() => setAdding(true)}
          className="flex items-center gap-2 px-4 py-2 bg-sky-500 text-white text-sm rounded-lg hover:bg-sky-600">
          <Plus size={15} /> Добавить бота
        </button>
      </div>

      {adding && (
        <div className="bg-white border border-slate-200 rounded-xl p-4 flex gap-3">
          <input value={token} onChange={e => setToken(e.target.value)}
            placeholder="Вставьте токен бота от @BotFather"
            className="flex-1 px-4 py-2 border border-slate-200 rounded-lg text-sm outline-none focus:border-sky-500" />
          <button onClick={() => addBot.mutate()} disabled={!token || addBot.isPending}
            className="px-4 py-2 bg-sky-500 text-white text-sm rounded-lg disabled:opacity-40">
            {addBot.isPending ? 'Добавление...' : 'Добавить'}
          </button>
          <button onClick={() => setAdding(false)} className="px-3 py-2 text-slate-500 hover:text-slate-700 text-sm">Отмена</button>
        </div>
      )}
      {addBot.isError && <p className="text-red-500 text-sm">Ошибка: неверный токен или бот недоступен</p>}

      <div className="grid gap-4">
        {(bots ?? []).map((b: any) => (
          <div key={b.id} className="bg-white border border-slate-200 rounded-xl p-5 flex items-center gap-4">
            <div className="w-10 h-10 bg-sky-100 rounded-full flex items-center justify-center">
              <Bot size={18} className="text-sky-600" />
            </div>
            <div className="flex-1">
              <p className="font-medium text-slate-800">@{b.username ?? b.firstName}</p>
              <p className="text-xs text-slate-400">ID: {b.telegramId} · {b._count?.conversations ?? 0} диалогов</p>
              <div className="flex gap-2 mt-1">
                <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${b.isActive ? 'bg-green-100 text-green-700' : 'bg-slate-100 text-slate-500'}`}>
                  {b.isActive ? 'Активен' : 'Неактивен'}
                </span>
                <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${b.webhookSet ? 'bg-blue-100 text-blue-700' : 'bg-orange-100 text-orange-600'}`}>
                  {b.webhookSet ? 'Webhook ✓' : 'Без webhook'}
                </span>
              </div>
            </div>
            <button onClick={() => deleteBot.mutate(b.id)}
              className="p-2 text-slate-400 hover:text-red-500 rounded-lg hover:bg-red-50 transition-colors">
              <Trash2 size={16} />
            </button>
          </div>
        ))}
        {(bots ?? []).length === 0 && (
          <div className="text-center py-12 text-slate-400">
            <Bot size={36} className="mx-auto mb-3 opacity-30" />
            <p>Нет ботов. Добавьте первый бот по токену.</p>
          </div>
        )}
      </div>
    </div>
  );
}
