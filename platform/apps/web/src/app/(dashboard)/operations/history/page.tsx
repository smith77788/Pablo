'use client';
import { Clock, CheckCircle, XCircle } from 'lucide-react';

const STATUS_COLOR: Record<string, string> = {
  COMPLETED: 'bg-green-100 text-green-700',
  FAILED: 'bg-red-100 text-red-700',
  CANCELLED: 'bg-slate-100 text-slate-600',
};

const STATUS_ICON: Record<string, React.ReactNode> = {
  COMPLETED: <CheckCircle size={13} className="text-green-500" />,
  FAILED: <XCircle size={13} className="text-red-500" />,
  CANCELLED: <Clock size={13} className="text-slate-400" />,
};

const MOCK_HISTORY = [
  { id: '1', name: 'Proxy health check', type: 'HEALTH', status: 'COMPLETED', duration: '2 мин', completedAt: '4 ч назад', result: '5/5 OK' },
  { id: '2', name: 'Warm-up accounts #3', type: 'WARMUP', status: 'COMPLETED', duration: '45 мин', completedAt: '5 ч назад', result: '8 аккаунтов' },
  { id: '3', name: 'Scrape competitors', type: 'SCRAPE', status: 'FAILED', duration: '3 мин', completedAt: '3 ч назад', result: 'Error: rate limit' },
  { id: '4', name: 'Broadcast "Апрель"', type: 'BROADCAST', status: 'COMPLETED', duration: '12 мин', completedAt: '1 д назад', result: '1,240 доставлено' },
  { id: '5', name: 'Mass follow v2', type: 'FOLLOW', status: 'CANCELLED', duration: '—', completedAt: '2 д назад', result: 'Отменено пользователем' },
];

export default function HistoryPage() {
  return (
    <div className="p-6 space-y-6">
      <div>
        <h1 className="text-xl font-bold text-slate-800">История операций</h1>
        <p className="text-sm text-slate-400 mt-0.5">Завершённые, провальные и отменённые операции</p>
      </div>

      <div className="bg-white rounded-xl border border-slate-200">
        <div className="p-4 border-b border-slate-100 flex items-center gap-2">
          <Clock size={15} className="text-slate-400" />
          <h2 className="font-semibold text-slate-800">История</h2>
          <span className="ml-2 text-xs text-slate-400 bg-slate-100 px-2 py-0.5 rounded-full">{MOCK_HISTORY.length}</span>
        </div>
        <table className="w-full text-sm">
          <thead className="bg-slate-50">
            <tr>
              <th className="text-left px-4 py-3 font-medium text-slate-500">Название</th>
              <th className="text-left px-4 py-3 font-medium text-slate-500">Тип</th>
              <th className="text-left px-4 py-3 font-medium text-slate-500">Статус</th>
              <th className="text-left px-4 py-3 font-medium text-slate-500">Длительность</th>
              <th className="text-left px-4 py-3 font-medium text-slate-500">Завершена</th>
              <th className="text-left px-4 py-3 font-medium text-slate-500">Результат</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-50">
            {MOCK_HISTORY.map((op) => (
              <tr key={op.id} className="hover:bg-slate-50 transition-colors">
                <td className="px-4 py-3 font-medium text-slate-800">{op.name}</td>
                <td className="px-4 py-3">
                  <span className="text-xs font-medium px-2 py-0.5 rounded-full bg-slate-100 text-slate-600">{op.type}</span>
                </td>
                <td className="px-4 py-3">
                  <span className={`flex items-center gap-1 text-xs font-medium w-fit px-2 py-0.5 rounded-full ${STATUS_COLOR[op.status] ?? 'bg-slate-100 text-slate-600'}`}>
                    {STATUS_ICON[op.status]}
                    {op.status}
                  </span>
                </td>
                <td className="px-4 py-3 text-slate-600">{op.duration}</td>
                <td className="px-4 py-3 text-slate-400 text-xs">{op.completedAt}</td>
                <td className="px-4 py-3 text-slate-500 text-xs">{op.result}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
