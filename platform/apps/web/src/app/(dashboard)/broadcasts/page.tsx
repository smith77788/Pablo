'use client';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { authApi } from '@/lib/api';
import { Send, Play, Plus } from 'lucide-react';
import { format } from 'date-fns';
import { ru } from 'date-fns/locale';

interface BroadcastItem {
  id: string;
  botName: string;
  status: string;
  total: number;
  sent: number;
  failed: number;
  createdAt: string;
  preview: string;
}

const MOCK_BROADCASTS: BroadcastItem[] = [
  { id: '1', botName: '@MyBot', status: 'done', total: 1500, sent: 1480, failed: 20, createdAt: new Date().toISOString(), preview: 'Привет! Специальное предложение...' },
  { id: '2', botName: '@ShopBot', status: 'running', total: 800, sent: 450, failed: 5, createdAt: new Date(Date.now() - 3600000).toISOString(), preview: 'Скидка 50% только сегодня!' },
  { id: '3', botName: '@MyBot', status: 'pending', total: 2000, sent: 0, failed: 0, createdAt: new Date(Date.now() - 86400000).toISOString(), preview: 'Новое обновление приложения...' },
];

const STATUS_LABEL: Record<string, string> = {
  done: 'Завершена',
  COMPLETED: 'Завершена',
  running: 'Выполняется',
  RUNNING: 'Выполняется',
  pending: 'Ожидает',
  DRAFT: 'Черновик',
  SCHEDULED: 'Запланирована',
  cancelled: 'Отменена',
  CANCELLED: 'Отменена',
  PAUSED: 'Приостановлена',
};

const STATUS_COLOR: Record<string, string> = {
  done: 'bg-green-100 text-green-700',
  COMPLETED: 'bg-green-100 text-green-700',
  running: 'bg-blue-100 text-blue-700',
  RUNNING: 'bg-blue-100 text-blue-700',
  pending: 'bg-yellow-100 text-yellow-700',
  DRAFT: 'bg-slate-100 text-slate-600',
  SCHEDULED: 'bg-blue-100 text-blue-700',
  cancelled: 'bg-slate-100 text-slate-500',
  CANCELLED: 'bg-slate-100 text-slate-500',
  PAUSED: 'bg-orange-100 text-orange-600',
};

function normalizeItem(bc: any): BroadcastItem {
  return {
    id: bc.id,
    botName: bc.botName ?? (bc.bot ? `@${bc.bot.username ?? bc.bot.firstName}` : '—'),
    status: bc.status,
    total: bc.total ?? bc.totalCount ?? 0,
    sent: bc.sent ?? bc.sentCount ?? 0,
    failed: bc.failed ?? bc.failedCount ?? 0,
    createdAt: bc.createdAt,
    preview: bc.preview ?? (typeof bc.message === 'object' ? bc.message?.text ?? '' : bc.message ?? ''),
  };
}

function isDoneStatus(status: string) {
  return status === 'done' || status === 'COMPLETED';
}

function canLaunch(status: string) {
  return status === 'DRAFT' || status === 'SCHEDULED' || status === 'pending';
}

export default function BroadcastsPage() {
  const qc = useQueryClient();

  const { data: rawBcs, isError } = useQuery({
    queryKey: ['broadcasts'],
    queryFn: () => authApi.get('/broadcasts'),
  });

  const launch = useMutation({
    mutationFn: (id: string) => authApi.post(`/broadcasts/${id}/launch`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['broadcasts'] }),
  });

  const apiBcs: BroadcastItem[] = Array.isArray(rawBcs) ? rawBcs.map(normalizeItem) : [];
  const broadcasts: BroadcastItem[] = isError || apiBcs.length === 0 ? MOCK_BROADCASTS : apiBcs;
  const usingMock = isError || apiBcs.length === 0;

  // ─── Metrics ───────────────────────────────────────────────────────────────
  const totalBroadcasts = broadcasts.length;
  const completedBroadcasts = broadcasts.filter(b => isDoneStatus(b.status)).length;
  const usersReached = broadcasts
    .filter(b => isDoneStatus(b.status))
    .reduce((sum, b) => sum + b.sent, 0);

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-slate-800">Рассылки</h1>
          <p className="text-sm text-slate-400 mt-0.5">История кампаний и рассылок</p>
        </div>
        <a
          href="/broadcasts/new"
          className="flex items-center gap-2 px-4 py-2 bg-sky-500 text-white text-sm font-medium rounded-lg hover:bg-sky-600 transition-colors"
        >
          <Plus size={15} /> Новая рассылка
        </a>
      </div>

      {/* Mock data notice */}
      {usingMock && (
        <div className="bg-orange-50 border border-orange-200 rounded-lg px-4 py-2 text-sm text-orange-600">
          API недоступен — показаны демо-данные
        </div>
      )}

      {/* Metrics */}
      <div className="grid grid-cols-3 gap-4">
        <div className="bg-white border border-slate-200 rounded-xl p-5">
          <p className="text-xs text-slate-400 font-medium uppercase tracking-wide">Всего рассылок</p>
          <p className="text-3xl font-bold text-slate-800 mt-1">{totalBroadcasts}</p>
        </div>
        <div className="bg-white border border-slate-200 rounded-xl p-5">
          <p className="text-xs text-slate-400 font-medium uppercase tracking-wide">Завершённых</p>
          <p className="text-3xl font-bold text-green-600 mt-1">{completedBroadcasts}</p>
        </div>
        <div className="bg-white border border-slate-200 rounded-xl p-5">
          <p className="text-xs text-slate-400 font-medium uppercase tracking-wide">Пользователей охвачено</p>
          <p className="text-3xl font-bold text-sky-600 mt-1">{usersReached.toLocaleString('ru')}</p>
        </div>
      </div>

      {/* Table */}
      <div className="bg-white border border-slate-200 rounded-xl overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-slate-100 bg-slate-50">
              <th className="text-left px-5 py-3 text-xs font-semibold text-slate-500 uppercase tracking-wide">Бот</th>
              <th className="text-left px-5 py-3 text-xs font-semibold text-slate-500 uppercase tracking-wide">Статус</th>
              <th className="text-left px-5 py-3 text-xs font-semibold text-slate-500 uppercase tracking-wide w-52">Прогресс</th>
              <th className="text-left px-5 py-3 text-xs font-semibold text-slate-500 uppercase tracking-wide">Дата</th>
              <th className="text-left px-5 py-3 text-xs font-semibold text-slate-500 uppercase tracking-wide">Превью</th>
              <th className="px-5 py-3" />
            </tr>
          </thead>
          <tbody>
            {broadcasts.map((bc) => {
              const progressPct = bc.total > 0 ? Math.round((bc.sent / bc.total) * 100) : 0;
              return (
                <tr key={bc.id} className="border-b border-slate-100 last:border-0 hover:bg-slate-50 transition-colors">
                  {/* Бот */}
                  <td className="px-5 py-4">
                    <div className="flex items-center gap-2">
                      <div className="w-7 h-7 bg-sky-100 rounded-full flex items-center justify-center shrink-0">
                        <Send size={13} className="text-sky-600" />
                      </div>
                      <span className="font-medium text-slate-700">{bc.botName}</span>
                    </div>
                  </td>

                  {/* Статус */}
                  <td className="px-5 py-4">
                    <span className={`text-xs px-2.5 py-1 rounded-full font-medium ${STATUS_COLOR[bc.status] ?? 'bg-slate-100 text-slate-500'}`}>
                      {STATUS_LABEL[bc.status] ?? bc.status}
                    </span>
                  </td>

                  {/* Прогресс */}
                  <td className="px-5 py-4">
                    <div className="space-y-1">
                      <div className="flex justify-between text-xs text-slate-500">
                        <span>{bc.sent.toLocaleString('ru')} / {bc.total.toLocaleString('ru')}</span>
                        <span>{progressPct}%</span>
                      </div>
                      <div className="h-1.5 bg-slate-100 rounded-full overflow-hidden w-40">
                        <div
                          className={`h-full rounded-full transition-all ${
                            isDoneStatus(bc.status) ? 'bg-green-500' :
                            bc.status === 'running' || bc.status === 'RUNNING' ? 'bg-blue-500' :
                            'bg-slate-300'
                          }`}
                          style={{ width: `${progressPct}%` }}
                        />
                      </div>
                    </div>
                  </td>

                  {/* Дата */}
                  <td className="px-5 py-4 text-slate-500 whitespace-nowrap">
                    {format(new Date(bc.createdAt), 'd MMM HH:mm', { locale: ru })}
                  </td>

                  {/* Превью */}
                  <td className="px-5 py-4 text-slate-400 max-w-xs truncate">
                    {bc.preview.length > 50 ? bc.preview.slice(0, 50) + '…' : bc.preview}
                  </td>

                  {/* Действия */}
                  <td className="px-5 py-4">
                    {canLaunch(bc.status) && !usingMock && (
                      <button
                        onClick={() => launch.mutate(bc.id)}
                        disabled={launch.isPending}
                        className="flex items-center gap-1.5 px-3 py-1.5 bg-green-50 text-green-700 text-xs font-medium rounded-lg hover:bg-green-100 transition-colors disabled:opacity-40"
                      >
                        <Play size={12} /> Запустить
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>

        {broadcasts.length === 0 && (
          <div className="text-center py-12 text-slate-400">
            <Send size={36} className="mx-auto mb-3 opacity-30" />
            <p className="text-sm">Рассылок пока нет</p>
          </div>
        )}
      </div>
    </div>
  );
}
