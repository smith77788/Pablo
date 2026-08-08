'use client';
import { useQuery } from '@tanstack/react-query';
import { authApi } from '@/lib/api';
import { useState } from 'react';
import { Network, Plus, X } from 'lucide-react';

interface Cluster {
  id: string;
  name: string;
  description: string;
  accountsCount: number;
  assetsCount: number;
  healthScore: number;
  status: string;
}

const MOCK_CLUSTERS: Cluster[] = [
  { id: '1', name: 'Cluster A', description: 'Основной кластер для продуктовых аккаунтов', accountsCount: 12, assetsCount: 28, healthScore: 95, status: 'ACTIVE' },
  { id: '2', name: 'Cluster B', description: 'Вторичный кластер для рекламных операций', accountsCount: 8, assetsCount: 15, healthScore: 72, status: 'WARNING' },
  { id: '3', name: 'Cluster C', description: 'Тестовый кластер', accountsCount: 3, assetsCount: 5, healthScore: 60, status: 'WARNING' },
  { id: '4', name: 'Cluster D', description: 'Резервный кластер', accountsCount: 0, assetsCount: 0, healthScore: 100, status: 'ACTIVE' },
];

function HealthBar({ value }: { value: number }) {
  const color = value >= 80 ? 'bg-green-500' : value >= 50 ? 'bg-yellow-500' : 'bg-red-500';
  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <span className="text-xs text-slate-500">Health Score</span>
        <span className="text-xs font-semibold text-slate-700">{value}%</span>
      </div>
      <div className="w-full h-2 bg-slate-100 rounded-full overflow-hidden">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${value}%` }} />
      </div>
    </div>
  );
}

export default function ClustersPage() {
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ name: '', description: '' });

  const { data, isLoading } = useQuery<Cluster[]>({
    queryKey: ['clusters'],
    queryFn: () => authApi.get('/clusters').then((r: any) => r.data ?? r),
    retry: false,
  });

  const clusters = data ?? MOCK_CLUSTERS;

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-slate-800">Кластеры</h1>
          <p className="text-sm text-slate-400 mt-0.5">Группировка и управление ресурсами</p>
        </div>
        <button
          onClick={() => setShowForm(true)}
          className="flex items-center gap-2 px-4 py-2 bg-sky-600 text-white text-sm rounded-lg hover:bg-sky-700 transition-colors"
        >
          <Plus size={15} /> Создать кластер
        </button>
      </div>

      {/* Inline create form */}
      {showForm && (
        <div className="bg-white rounded-xl border border-sky-200 p-5">
          <div className="flex items-center justify-between mb-4">
            <h3 className="font-semibold text-slate-800">Новый кластер</h3>
            <button onClick={() => setShowForm(false)} className="text-slate-400 hover:text-slate-600">
              <X size={16} />
            </button>
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">Название</label>
              <input
                type="text"
                value={form.name}
                onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
                placeholder="Cluster E"
                className="w-full text-sm border border-slate-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-sky-300"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">Описание</label>
              <input
                type="text"
                value={form.description}
                onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
                placeholder="Описание кластера"
                className="w-full text-sm border border-slate-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-sky-300"
              />
            </div>
          </div>
          <div className="flex gap-2 mt-4">
            <button className="px-4 py-2 bg-sky-600 text-white text-sm rounded-lg hover:bg-sky-700">
              Создать
            </button>
            <button onClick={() => setShowForm(false)} className="px-4 py-2 border border-slate-200 text-sm rounded-lg text-slate-600 hover:bg-slate-50">
              Отмена
            </button>
          </div>
        </div>
      )}

      {/* Cluster cards */}
      {isLoading ? (
        <div className="grid grid-cols-2 gap-4">
          {Array.from({ length: 4 }).map((_, i) => (
            <div key={i} className="bg-white rounded-xl border border-slate-200 p-5 h-44 animate-pulse">
              <div className="h-4 bg-slate-100 rounded w-1/3 mb-3" />
              <div className="h-3 bg-slate-100 rounded w-2/3 mb-6" />
              <div className="h-2 bg-slate-100 rounded w-full" />
            </div>
          ))}
        </div>
      ) : clusters.length === 0 ? (
        <div className="bg-white rounded-xl border border-slate-200 p-12 text-center text-slate-400 text-sm">
          No clusters found
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-4">
          {clusters.map((cluster) => (
            <div key={cluster.id} className="bg-white rounded-xl border border-slate-200 p-5 space-y-4">
              <div className="flex items-start justify-between">
                <div>
                  <div className="flex items-center gap-2">
                    <Network size={16} className="text-sky-600" />
                    <h3 className="font-semibold text-slate-800">{cluster.name}</h3>
                  </div>
                  <p className="text-xs text-slate-400 mt-1">{cluster.description}</p>
                </div>
                <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${cluster.status === 'ACTIVE' ? 'bg-green-100 text-green-700' : 'bg-yellow-100 text-yellow-700'}`}>
                  {cluster.status}
                </span>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div className="bg-slate-50 rounded-lg p-3 text-center">
                  <p className="text-2xl font-bold text-slate-800">{cluster.accountsCount}</p>
                  <p className="text-xs text-slate-400 mt-0.5">Аккаунтов</p>
                </div>
                <div className="bg-slate-50 rounded-lg p-3 text-center">
                  <p className="text-2xl font-bold text-slate-800">{cluster.assetsCount}</p>
                  <p className="text-xs text-slate-400 mt-0.5">Активов</p>
                </div>
              </div>

              <HealthBar value={cluster.healthScore} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
