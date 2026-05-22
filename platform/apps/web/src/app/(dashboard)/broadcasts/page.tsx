'use client';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { authApi } from '@/lib/api';
import { Send, Play, Plus } from 'lucide-react';
import { format } from 'date-fns';
import { ru } from 'date-fns/locale';

const STATUS_LABEL: Record<string, string> = {
  DRAFT: 'Черновик', SCHEDULED: 'Запланирована', RUNNING: 'Выполняется',
  COMPLETED: 'Завершена', CANCELLED: 'Отменена',
};
const STATUS_COLOR: Record<string, string> = {
  DRAFT: 'bg-slate-100 text-slate-600', SCHEDULED: 'bg-blue-100 text-blue-700',
  RUNNING: 'bg-yellow-100 text-yellow-700', COMPLETED: 'bg-green-100 text-green-700',
  CANCELLED: 'bg-red-100 text-red-600',
};

export default function BroadcastsPage() {
  const qc = useQueryClient();
  const { data: bcs } = useQuery({ queryKey: ['broadcasts'], queryFn: () => authApi.get('/broadcasts') });
  const launch = useMutation({
    mutationFn: (id: string) => authApi.post(`/broadcasts/${id}/launch`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['broadcasts'] }),
  });

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold text-slate-800">Рассылки</h1>
        <a href="/broadcasts/new" className="flex items-center gap-2 px-4 py-2 bg-sky-500 text-white text-sm rounded-lg hover:bg-sky-600">
          <Plus size={15} /> Новая рассылка
        </a>
      </div>

      <div className="space-y-3">
        {(bcs ?? []).map((bc: any) => (
          <div key={bc.id} className="bg-white border border-slate-200 rounded-xl p-5">
            <div className="flex items-center justify-between mb-2">
              <div>
                <span className="font-medium text-slate-800">{bc.name}</span>
                <span className="ml-2 text-xs text-slate-400">@{bc.bot?.username ?? bc.bot?.firstName}</span>
              </div>
              <span className={`text-xs px-2.5 py-1 rounded-full font-medium ${STATUS_COLOR[bc.status]}`}>
                {STATUS_LABEL[bc.status]}
              </span>
            </div>
            <div className="flex items-center gap-6 text-xs text-slate-500">
              <span>✅ {bc.sentCount ?? 0} отправлено</span>
              <span>❌ {bc.failedCount ?? 0} ошибок</span>
              <span>📋 {bc.totalCount ?? 0} всего</span>
              {bc.createdAt && <span>🕐 {format(new Date(bc.createdAt), 'd MMM HH:mm', { locale: ru })}</span>}
            </div>
            {(bc.status === 'DRAFT' || bc.status === 'SCHEDULED') && (
              <button onClick={() => launch.mutate(bc.id)}
                className="mt-3 flex items-center gap-1.5 px-3 py-1.5 bg-green-50 text-green-700 text-xs font-medium rounded-lg hover:bg-green-100">
                <Play size={12} /> Запустить
              </button>
            )}
          </div>
        ))}
        {(bcs ?? []).length === 0 && (
          <div className="text-center py-12 text-slate-400">
            <Send size={32} className="mx-auto mb-2 opacity-30" />
            <p className="text-sm">Рассылок пока нет</p>
          </div>
        )}
      </div>
    </div>
  );
}
