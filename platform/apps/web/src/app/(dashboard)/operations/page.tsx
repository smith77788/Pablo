'use client';
import { Play, Clock, CheckCircle, XCircle, Cog } from 'lucide-react';

const STATUS_COLOR: Record<string, string> = {
  RUNNING: 'bg-sky-100 text-sky-700',
  QUEUED: 'bg-yellow-100 text-yellow-700',
  COMPLETED: 'bg-green-100 text-green-700',
  FAILED: 'bg-red-100 text-red-700',
  DRAFT: 'bg-slate-100 text-slate-600',
};

const MOCK_OPS = [
  { id: '1', name: 'Mass follow Cluster A', type: 'FOLLOW', status: 'RUNNING', created: '10 мин назад', estimated: '25 мин' },
  { id: '2', name: 'Broadcast "Акция"', type: 'BROADCAST', status: 'QUEUED', created: '15 мин назад', estimated: '10 мин' },
  { id: '3', name: 'Warm-up accounts #3', type: 'WARMUP', status: 'COMPLETED', created: '2 ч назад', estimated: '-' },
  { id: '4', name: 'Scrape competitors', type: 'SCRAPE', status: 'FAILED', created: '3 ч назад', estimated: '-' },
  { id: '5', name: 'Channel post schedule', type: 'POST', status: 'QUEUED', created: '30 мин назад', estimated: '5 мин' },
  { id: '6', name: 'Proxy health check', type: 'HEALTH', status: 'COMPLETED', created: '4 ч назад', estimated: '-' },
];

const METRICS = [
  { label: 'Активных', value: 1, icon: Play, color: 'text-sky-600', bg: 'bg-sky-50' },
  { label: 'В очереди', value: 2, icon: Clock, color: 'text-yellow-600', bg: 'bg-yellow-50' },
  { label: 'Завершённых', value: 2, icon: CheckCircle, color: 'text-green-600', bg: 'bg-green-50' },
  { label: 'Провальных', value: 1, icon: XCircle, color: 'text-red-600', bg: 'bg-red-50' },
];

export default function OperationsPage() {
  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-xl font-bold text-slate-800">Операции</h1>
        <p className="text-sm text-slate-400 mt-0.5">Управление и мониторинг операций</p>
      </div>

      {/* Metric cards */}
      <div className="grid grid-cols-4 gap-4">
        {METRICS.map(({ label, value, icon: Icon, color, bg }) => (
          <div key={label} className="bg-white rounded-xl border border-slate-200 p-5">
            <div className="flex items-center justify-between mb-3">
              <span className="text-sm text-slate-500 font-medium">{label}</span>
              <span className={`w-8 h-8 rounded-lg flex items-center justify-center ${bg} ${color}`}>
                <Icon size={15} />
              </span>
            </div>
            <p className={`text-3xl font-bold ${color}`}>{value}</p>
          </div>
        ))}
      </div>

      {/* Recent operations table */}
      <div className="bg-white rounded-xl border border-slate-200">
        <div className="p-4 border-b border-slate-100 flex items-center gap-2">
          <Cog size={15} className="text-slate-400" />
          <h2 className="font-semibold text-slate-800">Последние операции</h2>
        </div>
        <table className="w-full text-sm">
          <thead className="bg-slate-50">
            <tr>
              <th className="text-left px-4 py-3 font-medium text-slate-500">Название</th>
              <th className="text-left px-4 py-3 font-medium text-slate-500">Тип</th>
              <th className="text-left px-4 py-3 font-medium text-slate-500">Статус</th>
              <th className="text-left px-4 py-3 font-medium text-slate-500">Создана</th>
              <th className="text-left px-4 py-3 font-medium text-slate-500">Оценка времени</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-50">
            {MOCK_OPS.map((op) => (
              <tr key={op.id} className="hover:bg-slate-50 transition-colors">
                <td className="px-4 py-3 font-medium text-slate-800">{op.name}</td>
                <td className="px-4 py-3">
                  <span className="text-xs font-medium px-2 py-0.5 rounded-full bg-slate-100 text-slate-600">{op.type}</span>
                </td>
                <td className="px-4 py-3">
                  <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${STATUS_COLOR[op.status] ?? 'bg-slate-100 text-slate-600'}`}>
                    {op.status}
                  </span>
                </td>
                <td className="px-4 py-3 text-slate-400 text-xs">{op.created}</td>
                <td className="px-4 py-3 text-slate-600 text-xs">{op.estimated}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
