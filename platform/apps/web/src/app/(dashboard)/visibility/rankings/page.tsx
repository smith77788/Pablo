'use client';
import { TrendingUp } from 'lucide-react';

export default function RankingsPage() {
  return (
    <div className="p-6 space-y-6">
      <div>
        <h1 className="text-xl font-bold text-slate-800">Позиции</h1>
        <p className="text-sm text-slate-400 mt-0.5">История и динамика позиций в поиске</p>
      </div>
      <div className="bg-white rounded-xl border border-slate-200 p-12 flex flex-col items-center gap-3 text-center">
        <TrendingUp size={32} className="text-slate-300" />
        <p className="font-semibold text-slate-600">Раздел в разработке</p>
        <p className="text-sm text-slate-400 max-w-xs">Детальная аналитика позиций будет доступна в следующем обновлении.</p>
      </div>
    </div>
  );
}
