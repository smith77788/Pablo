'use client';
import { List, Clock, Pause, X } from 'lucide-react';

const MOCK_QUEUE = [
  { id: '1', name: 'Broadcast "Акция"', type: 'BROADCAST', priority: 'HIGH', addedAt: '15 мин назад', estimatedStart: '5 мин' },
  { id: '2', name: 'Channel post schedule', type: 'POST', priority: 'NORMAL', addedAt: '30 мин назад', estimatedStart: '10 мин' },
  { id: '3', name: 'Warm-up batch #4', type: 'WARMUP', priority: 'LOW', addedAt: '1 ч назад', estimatedStart: '35 мин' },
];

const PRIORITY_COLOR: Record<string, string> = {
  HIGH: 'bg-red-100 text-red-700',
  NORMAL: 'bg-sky-100 text-sky-700',
  LOW: 'bg-slate-100 text-slate-600',
};

export default function QueuePage() {
  return (
    <div className="p-6 space-y-6">
      <div>
        <h1 className="text-xl font-bold text-slate-800">Очередь операций</h1>
        <p className="text-sm text-slate-400 mt-0.5">Операции, ожидающие выполнения</p>
      </div>

      <div className="bg-white rounded-xl border border-slate-200">
        <div className="p-4 border-b border-slate-100 flex items-center gap-2">
          <List size={15} className="text-slate-400" />
          <h2 className="font-semibold text-slate-800">Очередь</h2>
          <span className="ml-2 text-xs text-slate-400 bg-slate-100 px-2 py-0.5 rounded-full">{MOCK_QUEUE.length}</span>
        </div>
        {MOCK_QUEUE.length === 0 ? (
          <div className="p-12 text-center text-slate-400 text-sm">Очередь пуста</div>
        ) : (
          <div className="divide-y divide-slate-50">
            {MOCK_QUEUE.map((item, idx) => (
              <div key={item.id} className="px-5 py-4 flex items-center gap-4 hover:bg-slate-50 transition-colors">
                <div className="w-7 h-7 rounded-full bg-slate-100 text-slate-500 flex items-center justify-center text-xs font-bold">
                  {idx + 1}
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-semibold text-slate-800">{item.name}</p>
                  <p className="text-xs text-slate-400 mt-0.5">Добавлена {item.addedAt}</p>
                </div>
                <span className="text-xs font-medium px-2 py-0.5 rounded-full bg-slate-100 text-slate-600">{item.type}</span>
                <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${PRIORITY_COLOR[item.priority]}`}>{item.priority}</span>
                <div className="flex items-center gap-1 text-xs text-slate-400">
                  <Clock size={11} />
                  {item.estimatedStart}
                </div>
                <div className="flex gap-1.5">
                  <button title="Пауза" className="p-1.5 rounded-lg text-slate-400 hover:text-yellow-600 hover:bg-yellow-50 transition-colors">
                    <Pause size={13} />
                  </button>
                  <button title="Удалить из очереди" className="p-1.5 rounded-lg text-slate-400 hover:text-red-600 hover:bg-red-50 transition-colors">
                    <X size={13} />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
