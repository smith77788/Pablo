'use client';
import { FileText, Plus, Copy } from 'lucide-react';

const MOCK_TEMPLATES = [
  { id: '1', name: 'Daily Warm-up', type: 'WARMUP', description: 'Ежедневный прогрев аккаунтов в кластере', usedCount: 24, lastUsed: '3 д назад' },
  { id: '2', name: 'Promo Broadcast', type: 'BROADCAST', description: 'Рассылка промо-сообщений по базе пользователей', usedCount: 8, lastUsed: '1 нед назад' },
  { id: '3', name: 'Health Check All', type: 'HEALTH', description: 'Проверка здоровья всех прокси и аккаунтов', usedCount: 50, lastUsed: '1 д назад' },
];

export default function TemplatesPage() {
  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-slate-800">Шаблоны операций</h1>
          <p className="text-sm text-slate-400 mt-0.5">Готовые конфигурации для повторяющихся операций</p>
        </div>
        <button className="flex items-center gap-2 px-4 py-2 bg-sky-600 text-white text-sm rounded-lg hover:bg-sky-700 transition-colors">
          <Plus size={15} /> Создать шаблон
        </button>
      </div>

      <div className="bg-white rounded-xl border border-slate-200">
        <div className="p-4 border-b border-slate-100 flex items-center gap-2">
          <FileText size={15} className="text-slate-400" />
          <h2 className="font-semibold text-slate-800">Шаблоны</h2>
          <span className="ml-2 text-xs text-slate-400 bg-slate-100 px-2 py-0.5 rounded-full">{MOCK_TEMPLATES.length}</span>
        </div>
        <div className="divide-y divide-slate-50">
          {MOCK_TEMPLATES.map((tpl) => (
            <div key={tpl.id} className="px-5 py-4 flex items-center gap-4 hover:bg-slate-50 transition-colors">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-0.5">
                  <p className="text-sm font-semibold text-slate-800">{tpl.name}</p>
                  <span className="text-xs font-medium px-2 py-0.5 rounded-full bg-slate-100 text-slate-600">{tpl.type}</span>
                </div>
                <p className="text-xs text-slate-400">{tpl.description}</p>
              </div>
              <div className="text-right text-xs text-slate-400">
                <p>Использован {tpl.usedCount} раз</p>
                <p>Последний раз: {tpl.lastUsed}</p>
              </div>
              <button title="Использовать шаблон" className="flex items-center gap-1.5 px-3 py-1.5 text-xs border border-slate-200 rounded-lg text-slate-600 hover:bg-sky-50 hover:border-sky-200 hover:text-sky-700 transition-colors">
                <Copy size={12} /> Использовать
              </button>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
