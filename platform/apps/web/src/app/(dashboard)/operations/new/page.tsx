'use client';
import { useState } from 'react';
import { PlusCircle, Users, Server } from 'lucide-react';

const OP_TYPES = [
  { value: 'FOLLOW', label: 'Mass Follow' },
  { value: 'BROADCAST', label: 'Broadcast' },
  { value: 'WARMUP', label: 'Account Warm-up' },
  { value: 'SCRAPE', label: 'Scrape' },
  { value: 'POST', label: 'Channel Post' },
  { value: 'HEALTH', label: 'Health Check' },
  { value: 'CUSTOM', label: 'Custom' },
];

const MOCK_ACCOUNTS = [
  { id: 'a1', name: '+7 912 345-67-80', status: 'ACTIVE' },
  { id: 'a2', name: '+7 987 654-32-10', status: 'ACTIVE' },
  { id: 'a3', name: '@account_promo', status: 'LIMITED' },
];

const MOCK_CLUSTERS = [
  { id: 'c1', name: 'Cluster A' },
  { id: 'c2', name: 'Cluster B' },
  { id: 'c3', name: 'Cluster C' },
];

export default function NewOperationPage() {
  const [form, setForm] = useState({
    name: '',
    type: '',
    description: '',
  });
  const [selectedAccounts, setSelectedAccounts] = useState<Set<string>>(new Set());
  const [selectedClusters, setSelectedClusters] = useState<Set<string>>(new Set());

  const toggleAccount = (id: string) => {
    setSelectedAccounts((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  };

  const toggleCluster = (id: string) => {
    setSelectedClusters((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  };

  return (
    <div className="p-6 space-y-6 max-w-3xl">
      {/* Header */}
      <div className="flex items-center gap-3">
        <PlusCircle size={20} className="text-sky-600" />
        <div>
          <h1 className="text-xl font-bold text-slate-800">Создать операцию</h1>
          <p className="text-sm text-slate-400 mt-0.5">Настройте параметры новой операции</p>
        </div>
      </div>

      {/* Form */}
      <div className="bg-white rounded-xl border border-slate-200 p-6 space-y-5">
        <div>
          <label className="block text-sm font-medium text-slate-700 mb-1.5">Название операции</label>
          <input
            type="text"
            value={form.name}
            onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
            placeholder="Mass follow Cluster A"
            className="w-full text-sm border border-slate-200 rounded-lg px-3 py-2.5 focus:outline-none focus:ring-2 focus:ring-sky-300"
          />
        </div>

        <div>
          <label className="block text-sm font-medium text-slate-700 mb-1.5">Тип операции</label>
          <select
            value={form.type}
            onChange={(e) => setForm((f) => ({ ...f, type: e.target.value }))}
            className="w-full text-sm border border-slate-200 rounded-lg px-3 py-2.5 bg-white focus:outline-none focus:ring-2 focus:ring-sky-300 text-slate-700"
          >
            <option value="">Выберите тип...</option>
            {OP_TYPES.map((t) => (
              <option key={t.value} value={t.value}>{t.label}</option>
            ))}
          </select>
        </div>

        <div>
          <label className="block text-sm font-medium text-slate-700 mb-1.5">Описание</label>
          <textarea
            value={form.description}
            onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
            placeholder="Подробное описание операции..."
            rows={3}
            className="w-full text-sm border border-slate-200 rounded-lg px-3 py-2.5 focus:outline-none focus:ring-2 focus:ring-sky-300 resize-none"
          />
        </div>
      </div>

      {/* Account selection */}
      <div className="bg-white rounded-xl border border-slate-200 p-5">
        <div className="flex items-center gap-2 mb-4">
          <Users size={15} className="text-slate-400" />
          <h3 className="font-semibold text-slate-800">Аккаунты</h3>
          <span className="text-xs text-slate-400">(выберите аккаунты для выполнения)</span>
        </div>
        <div className="space-y-2">
          {MOCK_ACCOUNTS.map((account) => (
            <label
              key={account.id}
              className="flex items-center gap-3 p-3 rounded-lg border border-slate-100 hover:border-sky-200 hover:bg-sky-50 cursor-pointer transition-colors"
            >
              <input
                type="checkbox"
                checked={selectedAccounts.has(account.id)}
                onChange={() => toggleAccount(account.id)}
                className="rounded border-slate-300"
              />
              <span className="text-sm font-medium text-slate-700">{account.name}</span>
              <span className={`ml-auto text-xs font-medium px-2 py-0.5 rounded-full ${account.status === 'ACTIVE' ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'}`}>
                {account.status}
              </span>
            </label>
          ))}
        </div>
      </div>

      {/* Cluster selection */}
      <div className="bg-white rounded-xl border border-slate-200 p-5">
        <div className="flex items-center gap-2 mb-4">
          <Server size={15} className="text-slate-400" />
          <h3 className="font-semibold text-slate-800">Кластеры</h3>
          <span className="text-xs text-slate-400">(или выберите целые кластеры)</span>
        </div>
        <div className="space-y-2">
          {MOCK_CLUSTERS.map((cluster) => (
            <label
              key={cluster.id}
              className="flex items-center gap-3 p-3 rounded-lg border border-slate-100 hover:border-sky-200 hover:bg-sky-50 cursor-pointer transition-colors"
            >
              <input
                type="checkbox"
                checked={selectedClusters.has(cluster.id)}
                onChange={() => toggleCluster(cluster.id)}
                className="rounded border-slate-300"
              />
              <span className="text-sm font-medium text-slate-700">{cluster.name}</span>
            </label>
          ))}
        </div>
      </div>

      {/* Actions */}
      <div className="flex items-center gap-3">
        <button
          disabled={!form.name || !form.type}
          className="px-5 py-2.5 bg-sky-600 text-white text-sm rounded-lg hover:bg-sky-700 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
        >
          Создать черновик
        </button>
        <button className="px-5 py-2.5 border border-slate-200 text-sm rounded-lg text-slate-600 hover:bg-slate-50 transition-colors">
          Отмена
        </button>
      </div>
    </div>
  );
}
