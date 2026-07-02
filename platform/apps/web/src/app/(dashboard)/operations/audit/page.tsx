'use client';
import { BookOpen, User, Cog, Shield } from 'lucide-react';

const AUDIT_TYPES: Record<string, string> = {
  CREATE: 'bg-green-100 text-green-700',
  UPDATE: 'bg-sky-100 text-sky-700',
  DELETE: 'bg-red-100 text-red-700',
  EXECUTE: 'bg-violet-100 text-violet-700',
  LOGIN: 'bg-slate-100 text-slate-600',
};

const MOCK_AUDIT = [
  { id: '1', action: 'EXECUTE', entity: 'Operation', detail: 'Запущена операция "Proxy health check"', user: 'admin', ip: '192.168.1.1', at: '4 ч назад' },
  { id: '2', action: 'CREATE', entity: 'Proxy', detail: 'Добавлен прокси 45.76.12.34:1080', user: 'admin', ip: '192.168.1.1', at: '5 ч назад' },
  { id: '3', action: 'UPDATE', entity: 'Account', detail: 'Статус аккаунта +7912 изменён на WARNING', user: 'system', ip: '-', at: '6 ч назад' },
  { id: '4', action: 'DELETE', entity: 'Cluster', detail: 'Удалён кластер "Test Cluster"', user: 'admin', ip: '192.168.1.1', at: '1 д назад' },
  { id: '5', action: 'LOGIN', entity: 'Auth', detail: 'Успешный вход в систему', user: 'admin', ip: '192.168.1.1', at: '1 д назад' },
  { id: '6', action: 'CREATE', entity: 'Account', detail: 'Добавлен аккаунт +7987654321', user: 'admin', ip: '192.168.1.1', at: '2 д назад' },
];

const ENTITY_ICON: Record<string, React.ReactNode> = {
  Operation: <Cog size={13} />,
  Account: <Shield size={13} />,
  Auth: <User size={13} />,
  Proxy: <Cog size={13} />,
  Cluster: <Cog size={13} />,
};

export default function AuditPage() {
  return (
    <div className="p-6 space-y-6">
      <div>
        <h1 className="text-xl font-bold text-slate-800">Журнал аудита</h1>
        <p className="text-sm text-slate-400 mt-0.5">Все действия в системе с полной трассировкой</p>
      </div>

      <div className="bg-white rounded-xl border border-slate-200">
        <div className="p-4 border-b border-slate-100 flex items-center gap-2">
          <BookOpen size={15} className="text-slate-400" />
          <h2 className="font-semibold text-slate-800">Аудит событий</h2>
          <span className="ml-2 text-xs text-slate-400 bg-slate-100 px-2 py-0.5 rounded-full">{MOCK_AUDIT.length}</span>
        </div>
        <table className="w-full text-sm">
          <thead className="bg-slate-50">
            <tr>
              <th className="text-left px-4 py-3 font-medium text-slate-500">Действие</th>
              <th className="text-left px-4 py-3 font-medium text-slate-500">Сущность</th>
              <th className="text-left px-4 py-3 font-medium text-slate-500">Детали</th>
              <th className="text-left px-4 py-3 font-medium text-slate-500">Пользователь</th>
              <th className="text-left px-4 py-3 font-medium text-slate-500">IP</th>
              <th className="text-left px-4 py-3 font-medium text-slate-500">Время</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-50">
            {MOCK_AUDIT.map((entry) => (
              <tr key={entry.id} className="hover:bg-slate-50 transition-colors">
                <td className="px-4 py-3">
                  <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${AUDIT_TYPES[entry.action] ?? 'bg-slate-100 text-slate-600'}`}>
                    {entry.action}
                  </span>
                </td>
                <td className="px-4 py-3">
                  <span className="flex items-center gap-1.5 text-slate-600 text-xs">
                    {ENTITY_ICON[entry.entity] ?? <Cog size={13} />}
                    {entry.entity}
                  </span>
                </td>
                <td className="px-4 py-3 text-slate-700 text-xs max-w-xs truncate">{entry.detail}</td>
                <td className="px-4 py-3">
                  <span className="flex items-center gap-1.5 text-slate-600 text-xs">
                    <User size={11} />
                    {entry.user}
                  </span>
                </td>
                <td className="px-4 py-3 font-mono text-xs text-slate-400">{entry.ip}</td>
                <td className="px-4 py-3 text-slate-400 text-xs">{entry.at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
