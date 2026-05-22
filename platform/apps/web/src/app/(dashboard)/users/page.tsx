'use client';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import { authApi } from '@/lib/api';
import { Users, Search } from 'lucide-react';
import { format } from 'date-fns';

export default function UsersPage() {
  const [search, setSearch] = useState('');
  const { data, isLoading } = useQuery({
    queryKey: ['users', search],
    queryFn: () => authApi.get(`/users?search=${search}&limit=100`),
    placeholderData: (prev) => prev,
  });

  const users = data?.items ?? [];

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold text-slate-800">CRM — Пользователи</h1>
        <span className="text-sm text-slate-400">{data?.total ?? 0} всего</span>
      </div>

      <div className="relative">
        <Search size={15} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
        <input value={search} onChange={e => setSearch(e.target.value)}
          placeholder="Поиск по имени или username..."
          className="w-full pl-9 pr-4 py-2.5 border border-slate-200 rounded-lg text-sm outline-none focus:border-sky-500" />
      </div>

      <div className="bg-white border border-slate-200 rounded-xl overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-slate-50 border-b border-slate-200">
            <tr>
              <th className="px-4 py-3 text-left text-slate-500 font-medium">Пользователь</th>
              <th className="px-4 py-3 text-left text-slate-500 font-medium">Username</th>
              <th className="px-4 py-3 text-left text-slate-500 font-medium">Язык</th>
              <th className="px-4 py-3 text-left text-slate-500 font-medium">Теги</th>
              <th className="px-4 py-3 text-left text-slate-500 font-medium">Последний визит</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {users.map((u: any) => (
              <tr key={u.id} className="hover:bg-slate-50 transition-colors">
                <td className="px-4 py-3">
                  <div className="flex items-center gap-2">
                    <div className="w-7 h-7 bg-slate-100 rounded-full flex items-center justify-center text-xs font-medium text-slate-600">
                      {(u.firstName?.[0] ?? u.username?.[0] ?? '?').toUpperCase()}
                    </div>
                    <span className="font-medium text-slate-800">{u.firstName} {u.lastName}</span>
                  </div>
                </td>
                <td className="px-4 py-3 text-slate-500">{u.username ? `@${u.username}` : '—'}</td>
                <td className="px-4 py-3 text-slate-500">{u.languageCode ?? '—'}</td>
                <td className="px-4 py-3">
                  <div className="flex flex-wrap gap-1">
                    {(u.userTags ?? []).map((t: any) => (
                      <span key={t.tagId} className="px-2 py-0.5 rounded-full text-xs font-medium"
                        style={{ background: t.tag?.color + '20', color: t.tag?.color }}>
                        {t.tag?.name}
                      </span>
                    ))}
                  </div>
                </td>
                <td className="px-4 py-3 text-slate-400 text-xs">
                  {u.lastSeenAt ? format(new Date(u.lastSeenAt), 'dd.MM.yyyy HH:mm') : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {users.length === 0 && !isLoading && (
          <div className="text-center py-12 text-slate-400">
            <Users size={32} className="mx-auto mb-2 opacity-30" />
            <p className="text-sm">Нет пользователей</p>
          </div>
        )}
      </div>
    </div>
  );
}
