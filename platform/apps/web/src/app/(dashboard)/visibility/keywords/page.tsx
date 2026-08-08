'use client';
import { useState } from 'react';
import { Search, Plus, TrendingUp, TrendingDown, Minus } from 'lucide-react';

interface Keyword {
  id: string;
  keyword: string;
  language: string;
  group: string;
  position: number;
  delta: number;
  lastChecked: string;
}

const MOCK_KEYWORDS: Keyword[] = [
  { id: '1', keyword: 'купить телеграм аккаунт', language: 'RU', group: 'Покупка', position: 2, delta: 3, lastChecked: '1 ч назад' },
  { id: '2', keyword: 'телеграм прокси бесплатно', language: 'RU', group: 'Прокси', position: 1, delta: 5, lastChecked: '1 ч назад' },
  { id: '3', keyword: 'telegram bot api python', language: 'EN', group: 'API', position: 3, delta: 2, lastChecked: '2 ч назад' },
  { id: '4', keyword: 'telegram channel buy', language: 'EN', group: 'Покупка', position: 4, delta: -1, lastChecked: '2 ч назад' },
  { id: '5', keyword: 'tg account farm', language: 'EN', group: 'Аккаунты', position: 2, delta: 6, lastChecked: '3 ч назад' },
  { id: '6', keyword: 'купить бота телеграм', language: 'RU', group: 'Боты', position: 8, delta: -3, lastChecked: '3 ч назад' },
  { id: '7', keyword: 'телеграм рассылка сервис', language: 'RU', group: 'Рассылки', position: 12, delta: -5, lastChecked: '4 ч назад' },
  { id: '8', keyword: 'telegram spam tool', language: 'EN', group: 'Инструменты', position: 15, delta: 0, lastChecked: '5 ч назад' },
  { id: '9', keyword: 'telegram automation', language: 'EN', group: 'Автоматизация', position: 6, delta: 1, lastChecked: '6 ч назад' },
];

function DeltaBadge({ delta }: { delta: number }) {
  if (delta > 0) return (
    <span className="flex items-center gap-0.5 text-green-600 font-medium text-xs">
      <TrendingUp size={11} /> +{delta}
    </span>
  );
  if (delta < 0) return (
    <span className="flex items-center gap-0.5 text-red-600 font-medium text-xs">
      <TrendingDown size={11} /> {delta}
    </span>
  );
  return (
    <span className="flex items-center gap-0.5 text-slate-400 text-xs">
      <Minus size={11} /> 0
    </span>
  );
}

export default function KeywordsPage() {
  const [showForm, setShowForm] = useState(false);
  const [search, setSearch] = useState('');
  const [langFilter, setLangFilter] = useState('');
  const [newKw, setNewKw] = useState({ keyword: '', language: 'RU', group: '' });

  const filtered = MOCK_KEYWORDS.filter((k) => {
    if (search && !k.keyword.toLowerCase().includes(search.toLowerCase())) return false;
    if (langFilter && k.language !== langFilter) return false;
    return true;
  });

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-slate-800">Ключевые слова</h1>
          <p className="text-sm text-slate-400 mt-0.5">Отслеживание позиций в поиске</p>
        </div>
        <button
          onClick={() => setShowForm(!showForm)}
          className="flex items-center gap-2 px-4 py-2 bg-sky-600 text-white text-sm rounded-lg hover:bg-sky-700 transition-colors"
        >
          <Plus size={15} /> Добавить keyword
        </button>
      </div>

      {/* Add keyword form */}
      {showForm && (
        <div className="bg-white rounded-xl border border-sky-200 p-5">
          <h3 className="font-semibold text-slate-800 mb-4">Новое ключевое слово</h3>
          <div className="grid grid-cols-3 gap-4">
            <div className="col-span-1">
              <label className="block text-xs font-medium text-slate-600 mb-1">Ключевое слово</label>
              <input
                type="text"
                value={newKw.keyword}
                onChange={(e) => setNewKw((f) => ({ ...f, keyword: e.target.value }))}
                placeholder="telegram bot api"
                className="w-full text-sm border border-slate-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-sky-300"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">Язык</label>
              <select
                value={newKw.language}
                onChange={(e) => setNewKw((f) => ({ ...f, language: e.target.value }))}
                className="w-full text-sm border border-slate-200 rounded-lg px-3 py-2 bg-white focus:outline-none focus:ring-2 focus:ring-sky-300"
              >
                <option value="RU">RU</option>
                <option value="EN">EN</option>
                <option value="DE">DE</option>
                <option value="UA">UA</option>
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">Группа</label>
              <input
                type="text"
                value={newKw.group}
                onChange={(e) => setNewKw((f) => ({ ...f, group: e.target.value }))}
                placeholder="Покупка"
                className="w-full text-sm border border-slate-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-sky-300"
              />
            </div>
          </div>
          <div className="flex gap-2 mt-4">
            <button className="px-4 py-2 bg-sky-600 text-white text-sm rounded-lg hover:bg-sky-700">Добавить</button>
            <button onClick={() => setShowForm(false)} className="px-4 py-2 border border-slate-200 text-sm rounded-lg text-slate-600 hover:bg-slate-50">Отмена</button>
          </div>
        </div>
      )}

      {/* Filters */}
      <div className="flex items-center gap-3">
        <div className="relative flex-1 max-w-xs">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Поиск по ключевым словам..."
            className="w-full text-sm border border-slate-200 rounded-lg pl-9 pr-3 py-1.5 focus:outline-none focus:ring-2 focus:ring-sky-300"
          />
        </div>
        <select
          value={langFilter}
          onChange={(e) => setLangFilter(e.target.value)}
          className="text-sm border border-slate-200 rounded-lg px-3 py-1.5 bg-white text-slate-700 focus:outline-none focus:ring-2 focus:ring-sky-300"
        >
          <option value="">Все языки</option>
          <option value="RU">RU</option>
          <option value="EN">EN</option>
          <option value="DE">DE</option>
        </select>
        <span className="text-xs text-slate-400">{filtered.length} ключевых слов</span>
      </div>

      {/* Table */}
      <div className="bg-white rounded-xl border border-slate-200">
        <div className="p-4 border-b border-slate-100 flex items-center gap-2">
          <Search size={15} className="text-slate-400" />
          <h2 className="font-semibold text-slate-800">Keywords</h2>
        </div>
        <table className="w-full text-sm">
          <thead className="bg-slate-50">
            <tr>
              <th className="text-left px-4 py-3 font-medium text-slate-500">Ключевое слово</th>
              <th className="text-left px-4 py-3 font-medium text-slate-500">Язык</th>
              <th className="text-left px-4 py-3 font-medium text-slate-500">Группа</th>
              <th className="text-left px-4 py-3 font-medium text-slate-500">Позиция</th>
              <th className="text-left px-4 py-3 font-medium text-slate-500">Изменение</th>
              <th className="text-left px-4 py-3 font-medium text-slate-500">Проверено</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-50">
            {filtered.map((kw) => (
              <tr key={kw.id} className="hover:bg-slate-50 transition-colors">
                <td className="px-4 py-3 font-medium text-slate-800">{kw.keyword}</td>
                <td className="px-4 py-3">
                  <span className="text-xs font-medium px-2 py-0.5 rounded-full bg-slate-100 text-slate-600">{kw.language}</span>
                </td>
                <td className="px-4 py-3 text-slate-600">{kw.group}</td>
                <td className="px-4 py-3">
                  <span className={`font-bold ${kw.position <= 3 ? 'text-green-600' : kw.position <= 10 ? 'text-slate-800' : 'text-slate-400'}`}>
                    #{kw.position}
                  </span>
                </td>
                <td className="px-4 py-3">
                  <DeltaBadge delta={kw.delta} />
                </td>
                <td className="px-4 py-3 text-slate-400 text-xs">{kw.lastChecked}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
