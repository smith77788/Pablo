'use client';
import { TrendingUp, TrendingDown, Search, Eye } from 'lucide-react';

const METRICS = [
  { label: 'Отслеживается ключевых слов', value: '248', sub: '+12 за неделю', accent: 'bg-sky-50 text-sky-600' },
  { label: 'Средняя позиция', value: '4.2', sub: '↑ 0.3 за месяц', accent: 'bg-green-50 text-green-600' },
  { label: 'Топ растущих', value: '34', sub: 'ключевых слов', accent: 'bg-violet-50 text-violet-600' },
  { label: 'Топ падающих', value: '11', sub: 'ключевых слов', accent: 'bg-red-50 text-red-600' },
];

const TOP_GROWING = [
  { keyword: 'купить телеграм аккаунт', position: 2, delta: +3, group: 'Покупка' },
  { keyword: 'телеграм прокси', position: 1, delta: +5, group: 'Прокси' },
  { keyword: 'telegram bot api', position: 3, delta: +2, group: 'API' },
  { keyword: 'telegram channel buy', position: 4, delta: +4, group: 'Покупка' },
  { keyword: 'tg account farm', position: 2, delta: +6, group: 'Аккаунты' },
];

const TOP_FALLING = [
  { keyword: 'купить бота телеграм', position: 8, delta: -3, group: 'Боты' },
  { keyword: 'телеграм рассылка', position: 12, delta: -5, group: 'Рассылки' },
  { keyword: 'telegram spam', position: 15, delta: -2, group: 'Спам' },
];

export default function VisibilityPage() {
  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-xl font-bold text-slate-800">Видимость</h1>
        <p className="text-sm text-slate-400 mt-0.5">Мониторинг ключевых слов и позиций в поиске</p>
      </div>

      {/* Metric cards */}
      <div className="grid grid-cols-4 gap-4">
        {METRICS.map(({ label, value, sub, accent }) => (
          <div key={label} className="bg-white rounded-xl border border-slate-200 p-5">
            <div className="flex items-center justify-between mb-3">
              <span className="text-sm text-slate-500 font-medium">{label}</span>
              <span className={`w-8 h-8 rounded-lg flex items-center justify-center ${accent}`}>
                <Eye size={15} />
              </span>
            </div>
            <p className="text-3xl font-bold text-slate-800">{value}</p>
            <p className="text-xs text-slate-400 mt-1">{sub}</p>
          </div>
        ))}
      </div>

      <div className="grid grid-cols-2 gap-6">
        {/* Top Growing */}
        <div className="bg-white rounded-xl border border-slate-200">
          <div className="p-4 border-b border-slate-100 flex items-center gap-2">
            <TrendingUp size={15} className="text-green-500" />
            <h2 className="font-semibold text-slate-800">Топ растущих позиций</h2>
          </div>
          <div className="divide-y divide-slate-50">
            {TOP_GROWING.map((item) => (
              <div key={item.keyword} className="px-4 py-3 flex items-center gap-3 hover:bg-slate-50 transition-colors">
                <div className="w-6 h-6 rounded-full bg-green-100 text-green-700 flex items-center justify-center text-xs font-bold">
                  {item.position}
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-slate-700 truncate">{item.keyword}</p>
                  <p className="text-xs text-slate-400">{item.group}</p>
                </div>
                <span className="flex items-center gap-0.5 text-green-600 text-sm font-medium">
                  <TrendingUp size={12} />+{item.delta}
                </span>
              </div>
            ))}
          </div>
        </div>

        {/* Top Falling */}
        <div className="bg-white rounded-xl border border-slate-200">
          <div className="p-4 border-b border-slate-100 flex items-center gap-2">
            <TrendingDown size={15} className="text-red-500" />
            <h2 className="font-semibold text-slate-800">Топ падающих позиций</h2>
          </div>
          <div className="divide-y divide-slate-50">
            {TOP_FALLING.map((item) => (
              <div key={item.keyword} className="px-4 py-3 flex items-center gap-3 hover:bg-slate-50 transition-colors">
                <div className="w-6 h-6 rounded-full bg-red-100 text-red-700 flex items-center justify-center text-xs font-bold">
                  {item.position}
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-slate-700 truncate">{item.keyword}</p>
                  <p className="text-xs text-slate-400">{item.group}</p>
                </div>
                <span className="flex items-center gap-0.5 text-red-600 text-sm font-medium">
                  <TrendingDown size={12} />{item.delta}
                </span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Placeholder notice */}
      <div className="bg-sky-50 border border-sky-200 rounded-xl p-4 flex items-center gap-3">
        <Search size={16} className="text-sky-600 flex-shrink-0" />
        <p className="text-sm text-sky-700">Данные являются placeholder — реальные API запросы будут добавлены в следующей итерации.</p>
      </div>
    </div>
  );
}
