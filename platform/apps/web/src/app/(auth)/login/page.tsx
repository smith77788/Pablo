'use client';
import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { api } from '@/lib/api';

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true); setError('');
    try {
      const data = await api.post('/auth/login', { email, password });
      localStorage.setItem('token', data.accessToken);
      localStorage.setItem('refreshToken', data.refreshToken);
      router.push('/inbox');
    } catch (err: any) {
      setError(err?.response?.data?.message ?? 'Ошибка входа');
    } finally { setLoading(false); }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-slate-50">
      <div className="bg-white p-8 rounded-2xl shadow-sm border border-slate-200 w-full max-w-sm">
        <h1 className="text-2xl font-bold text-slate-800 mb-2">TG Platform</h1>
        <p className="text-slate-500 text-sm mb-6">Войдите в панель оператора</p>
        <form onSubmit={submit} className="space-y-4">
          <input
            type="email" placeholder="Email" value={email} onChange={e => setEmail(e.target.value)}
            className="w-full px-4 py-2.5 border border-slate-200 rounded-lg text-sm outline-none focus:border-sky-500"
            required
          />
          <input
            type="password" placeholder="Пароль" value={password} onChange={e => setPassword(e.target.value)}
            className="w-full px-4 py-2.5 border border-slate-200 rounded-lg text-sm outline-none focus:border-sky-500"
            required
          />
          {error && <p className="text-red-500 text-sm">{error}</p>}
          <button
            type="submit" disabled={loading}
            className="w-full bg-sky-500 hover:bg-sky-600 text-white py-2.5 rounded-lg text-sm font-medium transition-colors disabled:opacity-50"
          >
            {loading ? 'Вход...' : 'Войти'}
          </button>
        </form>
        <p className="mt-4 text-center text-sm text-slate-500">
          Нет аккаунта?{' '}
          <a href="/register" className="text-sky-500 hover:underline">Зарегистрироваться</a>
        </p>
      </div>
    </div>
  );
}
