'use client';
import { useQuery } from '@tanstack/react-query';
import { authApi } from '@/lib/api';
import { Shield, Plus } from 'lucide-react';

interface TelegramAccount {
  id: string;
  phone: string;
  username: string;
  status: string;
  trustScore: number;
  healthScore: number;
  floodCount7d: number;
  cluster: string;
  lastUsed: string;
}

const STATUS_COLOR: Record<string, string> = {
  ACTIVE: 'bg-green-100 text-green-700',
  WARNING: 'bg-yellow-100 text-yellow-700',
  LIMITED: 'bg-red-100 text-red-700',
  DISCONNECTED: 'bg-slate-100 text-slate-600',
  ARCHIVED: 'bg-slate-100 text-slate-400',
};

const MOCK_ACCOUNTS: TelegramAccount[] = [
  { id: '1', phone: '+7 912 345-67-80', username: '@account_main', status: 'ACTIVE', trustScore: 92, healthScore: 98, floodCount7d: 0, cluster: 'Cluster A', lastUsed: '2 мин назад' },
  { id: '2', phone: '+7 987 654-32-10', username: '@account_second', status: 'ACTIVE', trustScore: 85, healthScore: 90, floodCount7d: 2, cluster: 'Cluster A', lastUsed: '15 мин назад' },
  { id: '3', phone: '+7 916 123-45-67', username: '', status: 'WARNING', trustScore: 55, healthScore: 60, floodCount7d: 8, cluster: 'Cluster B', lastUsed: '3 ч назад' },
  { id: '4', phone: '+7 903 987-65-43', username: '@account_promo', status: 'LIMITED', trustScore: 32, healthScore: 35, floodCount7d: 24, cluster: 'Cluster B', lastUsed: '1 д назад' },
  { id: '5', phone: '+7 925 111-22-33', username: '', status: 'DISCONNECTED', trustScore: 0, healthScore: 0, floodCount7d: 0, cluster: '-', lastUsed: '5 дн назад' },
];

function ScoreBar({ value, color }: { value: number; color: string }) {
  return (
    <div className="flex items-center gap-2">
      <div className="w-16 h-1.5 bg-slate-100 rounded-full overflow-hidden">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${value}%` }} />
      </div>
      <span className="text-xs text-slate-500">{value}</span>
    </div>
  );
}

export default function TelegramAccountsPage() {
  const { data, isLoading } = useQuery<TelegramAccount[]>({
    queryKey: ['telegram-accounts'],
    queryFn: () => authApi.get('/accounts').then((r: any) => r.data ?? r),
    retry: false,
  });

  const accounts = data ?? MOCK_ACCOUNTS;

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-slate-800">Telegram Аккаунты</h1>
          <p className="text-sm text-slate-400 mt-0.5">Управление аккаунтами и мониторинг здоровья</p>
        </div>
        <button className="flex items-center gap-2 px-4 py-2 bg-sky-600 text-white text-sm rounded-lg hover:bg-sky-700 transition-colors">
          <Plus size={15} /> Добавить аккаунт
        </button>
      </div>

      {/* Summary cards */}
      <div className="grid grid-cols-4 gap-4">
        {[
          { label: 'Всего аккаунтов', value: accounts.length, color: 'text-slate-800' },
          { label: 'Активных', value: accounts.filter((a) => a.status === 'ACTIVE').length, color: 'text-green-700' },
          { label: 'Предупреждения', value: accounts.filter((a) => a.status === 'WARNING').length, color: 'text-yellow-700' },
          { label: 'Ограничены', value: accounts.filter((a) => a.status === 'LIMITED').length, color: 'text-red-700' },
        ].map(({ label, value, color }) => (
          <div key={label} className="bg-white rounded-xl border border-slate-200 p-4">
            <p className="text-xs text-slate-500 mb-1">{label}</p>
            <p className={`text-2xl font-bold ${color}`}>{value}</p>
          </div>
        ))}
      </div>

      {/* Table */}
      <div className="bg-white rounded-xl border border-slate-200">
        <div className="p-4 border-b border-slate-100 flex items-center gap-2">
          <Shield size={15} className="text-slate-400" />
          <h2 className="font-semibold text-slate-800">Аккаунты</h2>
          <span className="ml-2 text-xs text-slate-400 bg-slate-100 px-2 py-0.5 rounded-full">{accounts.length}</span>
        </div>

        {isLoading ? (
          <div className="p-6 space-y-3">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="h-12 bg-slate-100 rounded animate-pulse" />
            ))}
          </div>
        ) : accounts.length === 0 ? (
          <div className="p-12 text-center text-slate-400 text-sm">No accounts found</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="bg-slate-50">
              <tr>
                <th className="text-left px-4 py-3 font-medium text-slate-500">Телефон / Username</th>
                <th className="text-left px-4 py-3 font-medium text-slate-500">Статус</th>
                <th className="text-left px-4 py-3 font-medium text-slate-500">Trust Score</th>
                <th className="text-left px-4 py-3 font-medium text-slate-500">Health Score</th>
                <th className="text-left px-4 py-3 font-medium text-slate-500">Flood 7d</th>
                <th className="text-left px-4 py-3 font-medium text-slate-500">Кластер</th>
                <th className="text-left px-4 py-3 font-medium text-slate-500">Последняя активность</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-50">
              {accounts.map((account) => (
                <tr key={account.id} className="hover:bg-slate-50 transition-colors">
                  <td className="px-4 py-3">
                    <div>
                      <p className="font-medium text-slate-800">{account.phone}</p>
                      {account.username && (
                        <p className="text-xs text-slate-400">{account.username}</p>
                      )}
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${STATUS_COLOR[account.status] ?? 'bg-slate-100 text-slate-600'}`}>
                      {account.status}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    <ScoreBar
                      value={account.trustScore}
                      color={account.trustScore >= 75 ? 'bg-green-500' : account.trustScore >= 50 ? 'bg-yellow-500' : 'bg-red-500'}
                    />
                  </td>
                  <td className="px-4 py-3">
                    <ScoreBar
                      value={account.healthScore}
                      color={account.healthScore >= 75 ? 'bg-green-500' : account.healthScore >= 50 ? 'bg-yellow-500' : 'bg-red-500'}
                    />
                  </td>
                  <td className="px-4 py-3">
                    <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${account.floodCount7d === 0 ? 'bg-slate-100 text-slate-500' : account.floodCount7d < 10 ? 'bg-yellow-100 text-yellow-700' : 'bg-red-100 text-red-700'}`}>
                      {account.floodCount7d}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-slate-600">{account.cluster}</td>
                  <td className="px-4 py-3 text-slate-400 text-xs">{account.lastUsed}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
