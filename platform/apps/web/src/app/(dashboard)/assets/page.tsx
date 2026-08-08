'use client';
import { useQuery } from '@tanstack/react-query';
import { authApi } from '@/lib/api';
import { useState } from 'react';
import { Server, Plus, Filter } from 'lucide-react';

interface Asset {
  id: string;
  name: string;
  type: string;
  status: string;
  healthScore: number;
  cluster: string;
  lastActivity: string;
}

const STATUS_COLOR: Record<string, string> = {
  ACTIVE: 'bg-green-100 text-green-700',
  WARNING: 'bg-yellow-100 text-yellow-700',
  LIMITED: 'bg-red-100 text-red-700',
  DISCONNECTED: 'bg-slate-100 text-slate-600',
  ARCHIVED: 'bg-slate-100 text-slate-400',
};

const TYPE_COLOR: Record<string, string> = {
  ACCOUNT: 'bg-sky-100 text-sky-700',
  BOT: 'bg-violet-100 text-violet-700',
  CHANNEL: 'bg-orange-100 text-orange-700',
  PROXY: 'bg-teal-100 text-teal-700',
};

const MOCK_ASSETS: Asset[] = [
  { id: '1', name: '@main_bot', type: 'BOT', status: 'ACTIVE', healthScore: 98, cluster: 'Cluster A', lastActivity: '2 мин назад' },
  { id: '2', name: '+7912345678', type: 'ACCOUNT', status: 'ACTIVE', healthScore: 92, cluster: 'Cluster A', lastActivity: '5 мин назад' },
  { id: '3', name: '185.12.45.67:3128', type: 'PROXY', status: 'ACTIVE', healthScore: 87, cluster: 'Cluster B', lastActivity: '1 мин назад' },
  { id: '4', name: '@news_channel', type: 'CHANNEL', status: 'ACTIVE', healthScore: 100, cluster: 'Cluster A', lastActivity: '10 мин назад' },
  { id: '5', name: '+7987654321', type: 'ACCOUNT', status: 'WARNING', healthScore: 54, cluster: 'Cluster B', lastActivity: '2 ч назад' },
  { id: '6', name: '@promo_bot', type: 'BOT', status: 'DISCONNECTED', healthScore: 0, cluster: '-', lastActivity: '3 дня назад' },
  { id: '7', name: '91.234.56.78:1080', type: 'PROXY', status: 'LIMITED', healthScore: 30, cluster: 'Cluster C', lastActivity: '15 мин назад' },
];

export default function AssetsPage() {
  const [typeFilter, setTypeFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  const { data, isLoading } = useQuery<Asset[]>({
    queryKey: ['assets'],
    queryFn: () => authApi.get('/assets').then((r: any) => r.data ?? r),
    retry: false,
  });

  const assets = (data ?? MOCK_ASSETS).filter((a) => {
    if (typeFilter && a.type !== typeFilter) return false;
    if (statusFilter && a.status !== statusFilter) return false;
    return true;
  });

  const toggleSelect = (id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  };

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-slate-800">Реестр активов</h1>
          <p className="text-sm text-slate-400 mt-0.5">Все управляемые ресурсы платформы</p>
        </div>
        <button className="flex items-center gap-2 px-4 py-2 bg-sky-600 text-white text-sm rounded-lg hover:bg-sky-700 transition-colors">
          <Plus size={15} /> Добавить актив
        </button>
      </div>

      {/* Filters */}
      <div className="flex items-center gap-3">
        <div className="flex items-center gap-2 text-slate-500 text-sm">
          <Filter size={14} />
          <span>Фильтры:</span>
        </div>
        <select
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value)}
          className="text-sm border border-slate-200 rounded-lg px-3 py-1.5 bg-white text-slate-700 focus:outline-none focus:ring-2 focus:ring-sky-300"
        >
          <option value="">Все типы</option>
          <option value="ACCOUNT">Аккаунт</option>
          <option value="BOT">Бот</option>
          <option value="CHANNEL">Канал</option>
          <option value="PROXY">Прокси</option>
        </select>
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="text-sm border border-slate-200 rounded-lg px-3 py-1.5 bg-white text-slate-700 focus:outline-none focus:ring-2 focus:ring-sky-300"
        >
          <option value="">Все статусы</option>
          <option value="ACTIVE">Active</option>
          <option value="WARNING">Warning</option>
          <option value="LIMITED">Limited</option>
          <option value="DISCONNECTED">Disconnected</option>
          <option value="ARCHIVED">Archived</option>
        </select>
        {selectedIds.size > 0 && (
          <div className="ml-auto flex gap-2">
            <button className="text-sm px-3 py-1.5 border border-slate-200 rounded-lg text-slate-600 hover:bg-slate-50">
              Архивировать ({selectedIds.size})
            </button>
            <button className="text-sm px-3 py-1.5 border border-red-200 rounded-lg text-red-600 hover:bg-red-50">
              Удалить ({selectedIds.size})
            </button>
          </div>
        )}
      </div>

      {/* Table */}
      <div className="bg-white rounded-xl border border-slate-200">
        <div className="p-4 border-b border-slate-100 flex items-center gap-2">
          <Server size={15} className="text-slate-400" />
          <h2 className="font-semibold text-slate-800">Активы</h2>
          <span className="ml-2 text-xs text-slate-400 bg-slate-100 px-2 py-0.5 rounded-full">{assets.length}</span>
        </div>

        {isLoading ? (
          <div className="p-6 space-y-3">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="h-10 bg-slate-100 rounded animate-pulse" />
            ))}
          </div>
        ) : assets.length === 0 ? (
          <div className="p-12 text-center text-slate-400 text-sm">No assets found</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-slate-50">
              <tr>
                <th className="w-10 px-4 py-3">
                  <input
                    type="checkbox"
                    className="rounded border-slate-300"
                    onChange={(e) => {
                      if (e.target.checked) setSelectedIds(new Set(assets.map((a) => a.id)));
                      else setSelectedIds(new Set());
                    }}
                    checked={selectedIds.size === assets.length && assets.length > 0}
                  />
                </th>
                <th className="text-left px-4 py-3 font-medium text-slate-500">Название</th>
                <th className="text-left px-4 py-3 font-medium text-slate-500">Тип</th>
                <th className="text-left px-4 py-3 font-medium text-slate-500">Статус</th>
                <th className="text-left px-4 py-3 font-medium text-slate-500">Health Score</th>
                <th className="text-left px-4 py-3 font-medium text-slate-500">Кластер</th>
                <th className="text-left px-4 py-3 font-medium text-slate-500">Активность</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-50">
              {assets.map((asset) => (
                <tr key={asset.id} className="hover:bg-slate-50 transition-colors">
                  <td className="px-4 py-3">
                    <input
                      type="checkbox"
                      className="rounded border-slate-300"
                      checked={selectedIds.has(asset.id)}
                      onChange={() => toggleSelect(asset.id)}
                    />
                  </td>
                  <td className="px-4 py-3 font-medium text-slate-800">{asset.name}</td>
                  <td className="px-4 py-3">
                    <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${TYPE_COLOR[asset.type] ?? 'bg-slate-100 text-slate-600'}`}>
                      {asset.type}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${STATUS_COLOR[asset.status] ?? 'bg-slate-100 text-slate-600'}`}>
                      {asset.status}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      <div className="w-20 h-1.5 bg-slate-100 rounded-full overflow-hidden">
                        <div
                          className={`h-full rounded-full ${asset.healthScore >= 80 ? 'bg-green-500' : asset.healthScore >= 50 ? 'bg-yellow-500' : 'bg-red-500'}`}
                          style={{ width: `${asset.healthScore}%` }}
                        />
                      </div>
                      <span className="text-xs text-slate-500">{asset.healthScore}%</span>
                    </div>
                  </td>
                  <td className="px-4 py-3 text-slate-600">{asset.cluster}</td>
                  <td className="px-4 py-3 text-slate-400 text-xs">{asset.lastActivity}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
