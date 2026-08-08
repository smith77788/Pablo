'use client';
import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { api } from '@/lib/api';

export default function RegisterPage() {
  const router = useRouter();
  const [form, setForm] = useState({ tenantName: '', email: '', password: '' });
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true); setError('');
    try {
      const data = await api.post('/auth/register', form);
      localStorage.setItem('token', data.accessToken);
      localStorage.setItem('refreshToken', data.refreshToken);
      router.push('/inbox');
    } catch (err: any) {
      setError(err?.response?.data?.message ?? 'Ошибка регистрации');
    } finally { setLoading(false); }
  }

  const set = (k: string) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm(f => ({ ...f, [k]: e.target.value }));

  return (
    <div className="min-h-screen flex items-center justify-center bg-slate-50">
      <div className="bg-white p-8 rounded-2xl shadow-sm border border-slate-200 w-full max-w-sm">
        <h1 className="text-2xl font-bold text-slate-800 mb-6">Создать аккаунт</h1>
        <form onSubmit={submit} className="space-y-4">
          <input placeholder="Название компании" value={form.tenantName} onChange={set('tenantName')}
            className="w-full px-4 py-2.5 border border-slate-200 rounded-lg text-sm outline-none focus:border-sky-500" required />
          <input type="email" placeholder="Email" value={form.email} onChange={set('email')}
            className="w-full px-4 py-2.5 border border-slate-200 rounded-lg text-sm outline-none focus:border-sky-500" required />
          <input type="password" placeholder="Пароль (мин. 8 символов)" value={form.password} onChange={set('password')}
            className="w-full px-4 py-2.5 border border-slate-200 rounded-lg text-sm outline-none focus:border-sky-500" required minLength={8} />
          {error && <p className="text-red-500 text-sm">{error}</p>}
          <button type="submit" disabled={loading}
            className="w-full bg-sky-500 hover:bg-sky-600 text-white py-2.5 rounded-lg text-sm font-medium transition-colors disabled:opacity-50">
            {loading ? 'Создание...' : 'Создать аккаунт'}
          </button>
        </form>
        <p className="mt-4 text-center text-sm text-slate-500">
          Уже есть аккаунт? <a href="/login" className="text-sky-500 hover:underline">Войти</a>
        </p>
      </div>
    </div>
  );
}
