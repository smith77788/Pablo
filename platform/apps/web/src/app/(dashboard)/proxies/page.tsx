'use client';
import { useQuery } from '@tanstack/react-query';
import { authApi } from '@/lib/api';
import { useState } from 'react';
import { Wifi, Plus, X } from 'lucide-react';

interface Proxy {
  id: string;
  host: string;
  port: number;
  type: string;
  region: string;
  status: string;
  latencyMs: number;
  healthScore: number;
  assignedAccountsCount: number;
}

const STATUS_COLOR: Record<string, string> = {
  ACTIVE: 'bg-green-100 text-green-700',
  WARNING: 'bg-yellow-100 text-yellow-700',
  LIMITED: 'bg-red-100 text-red-700',
  DISCONNECTED: 'bg-slate-100 text-slate-600',
};

const MOCK_PROXIES: Proxy[] = [
  { id: '1', host: '185.12.45.67', port: 3128, type: 'HTTP', region: 'RU', status: 'ACTIVE', latencyMs: 45, healthScore: 98, assignedAccountsCount: 4 },
  { id: '2', host: '91.234.56.78', port: 1080, type: 'SOCKS5', region: 'DE', status: 'ACTIVE', latencyMs: 112, healthScore: 87, assignedAccountsCount: 2 },
  { id: '3', host: '104.21.34.56', port: 8080, type: 'HTTP', region: 'US', status: 'WARNING', latencyMs: 380, healthScore: 52, assignedAccountsCount: 1 },
  { id: '4', host: '172.67.89.12', port: 3128, type: 'HTTP', region: 'NL', status: 'DISCONNECTED', latencyMs: 0, healthScore: 0, assignedAccountsCount: 0 },
  { id: '5', host: '45.76.12.34', port: 1080, type: 'SOCKS5', region: 'UA', status: 'ACTIVE', latencyMs: 78, healthScore: 94, assignedAccountsCount: 3 },
];

export default function ProxiesPage() {
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState({ host: '', port: '', type: 'HTTP', username: '', password: '', region: '' });

  const { data, isLoading } = useQuery<Proxy[]>({
    queryKey: ['proxies'],
    queryFn: () => authApi.get('/proxies').then((r: any) => r.data ?? r),
    retry: false,
  });

  const proxies = data ?? MOCK_PROXIES;

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-slate-800">Прокси</h1>
          <p className="text-sm text-slate-400 mt-0.5">Управление прокси-серверами</p>
        </div>
        <button
          onClick={() => setShowForm(!showForm)}
          className="flex items-center gap-2 px-4 py-2 bg-sky-600 text-white text-sm rounded-lg hover:bg-sky-700 transition-colors"
        >
          <Plus size={15} /> Добавить прокси
        </button>
      </div>

      {/* Add form */}
      {showForm && (
        <div className="bg-white rounded-xl border border-sky-200 p-5">
          <div className="flex items-center justify-between mb-4">
            <h3 className="font-semibold text-slate-800">Новый прокси</h3>
            <button onClick={() => setShowForm(false)} className="text-slate-400 hover:text-slate-600">
              <X size={16} />
            </button>
          </div>
          <div className="grid grid-cols-3 gap-4">
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">Host</label>
              <input
                type="text"
                value={form.host}
                onChange={(e) => setForm((f) => ({ ...f, host: e.target.value }))}
                placeholder="185.12.45.67"
                className="w-full text-sm border border-slate-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-sky-300"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">Port</label>
              <input
                type="number"
                value={form.port}
                onChange={(e) => setForm((f) => ({ ...f, port: e.target.value }))}
                placeholder="3128"
                className="w-full text-sm border border-slate-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-sky-300"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">Тип</label>
              <select
                value={form.type}
                onChange={(e) => setForm((f) => ({ ...f, type: e.target.value }))}
                className="w-full text-sm border border-slate-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-sky-300 bg-white"
              >
                <option value="HTTP">HTTP</option>
                <option value="SOCKS5">SOCKS5</option>
                <option value="HTTPS">HTTPS</option>
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">Логин</label>
              <input
                type="text"
                value={form.username}
                onChange={(e) => setForm((f) => ({ ...f, username: e.target.value }))}
                placeholder="user"
                className="w-full text-sm border border-slate-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-sky-300"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">Пароль</label>
              <input
                type="password"
                value={form.password}
                onChange={(e) => setForm((f) => ({ ...f, password: e.target.value }))}
                placeholder="••••••"
                className="w-full text-sm border border-slate-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-sky-300"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-slate-600 mb-1">Регион</label>
              <input
                type="text"
                value={form.region}
                onChange={(e) => setForm((f) => ({ ...f, region: e.target.value }))}
                placeholder="RU"
                className="w-full text-sm border border-slate-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-sky-300"
              />
            </div>
          </div>
          <div className="flex gap-2 mt-4">
            <button className="px-4 py-2 bg-sky-600 text-white text-sm rounded-lg hover:bg-sky-700">
              Добавить
            </button>
            <button onClick={() => setShowForm(false)} className="px-4 py-2 border border-slate-200 text-sm rounded-lg text-slate-600 hover:bg-slate-50">
              Отмена
            </button>
          </div>
        </div>
      )}

      {/* Table */}
      <div className="bg-white rounded-xl border border-slate-200">
        <div className="p-4 border-b border-slate-100 flex items-center gap-2">
          <Wifi size={15} className="text-slate-400" />
          <h2 className="font-semibold text-slate-800">Прокси-серверы</h2>
          <span className="ml-2 text-xs text-slate-400 bg-slate-100 px-2 py-0.5 rounded-full">{proxies.length}</span>
        </div>

        {isLoading ? (
          <div className="p-6 space-y-3">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="h-10 bg-slate-100 rounded animate-pulse" />
            ))}
          </div>
        ) : proxies.length === 0 ? (
          <div className="p-12 text-center text-slate-400 text-sm">No proxies found</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-slate-50">
              <tr>
                <th className="text-left px-4 py-3 font-medium text-slate-500">Host:Port</th>
                <th className="text-left px-4 py-3 font-medium text-slate-500">Тип</th>
                <th className="text-left px-4 py-3 font-medium text-slate-500">Регион</th>
                <th className="text-left px-4 py-3 font-medium text-slate-500">Статус</th>
                <th className="text-left px-4 py-3 font-medium text-slate-500">Latency</th>
                <th className="text-left px-4 py-3 font-medium text-slate-500">Health</th>
                <th className="text-left px-4 py-3 font-medium text-slate-500">Аккаунтов</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-50">
              {proxies.map((proxy) => (
                <tr key={proxy.id} className="hover:bg-slate-50 transition-colors">
                  <td className="px-4 py-3 font-mono text-slate-800">{proxy.host}:{proxy.port}</td>
                  <td className="px-4 py-3">
                    <span className="text-xs font-medium px-2 py-0.5 rounded-full bg-slate-100 text-slate-600">
                      {proxy.type}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-slate-600">{proxy.region}</td>
                  <td className="px-4 py-3">
                    <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${STATUS_COLOR[proxy.status] ?? 'bg-slate-100 text-slate-600'}`}>
                      {proxy.status}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-slate-600">
                    {proxy.latencyMs > 0 ? (
                      <span className={proxy.latencyMs < 100 ? 'text-green-600' : proxy.latencyMs < 300 ? 'text-yellow-600' : 'text-red-600'}>
                        {proxy.latencyMs} мс
                      </span>
                    ) : (
                      <span className="text-slate-300">—</span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex items-center gap-2">
                      <div className="w-16 h-1.5 bg-slate-100 rounded-full overflow-hidden">
                        <div
                          className={`h-full rounded-full ${proxy.healthScore >= 80 ? 'bg-green-500' : proxy.healthScore >= 50 ? 'bg-yellow-500' : 'bg-red-500'}`}
                          style={{ width: `${proxy.healthScore}%` }}
                        />
                      </div>
                      <span className="text-xs text-slate-500">{proxy.healthScore}%</span>
                    </div>
                  </td>
                  <td className="px-4 py-3 text-slate-600">{proxy.assignedAccountsCount}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
