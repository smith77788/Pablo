'use client';
import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { LogIn } from 'lucide-react';
import { api } from '@/lib/api';
import { Button, Input, Label } from '@/components/ui';

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError('');
    try {
      const data = await api.post('/auth/login', { email, password });
      localStorage.setItem('token', data.accessToken);
      localStorage.setItem('refreshToken', data.refreshToken);
      router.push('/dashboard');
    } catch (err: any) {
      setError(err?.response?.data?.message ?? 'Неверный email или пароль');
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-bg px-4 py-10">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex flex-col items-center text-center">
          <span className="mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-accent text-2xl font-bold text-accent-fg shadow-md">
            i
          </span>
          <h1 className="text-2xl font-bold tracking-tight text-fg">Infragram</h1>
          <p className="mt-1 text-sm text-fg-muted">Операционная система Telegram-инфраструктуры</p>
        </div>

        <form
          onSubmit={submit}
          className="space-y-4 rounded-3xl border border-line bg-surface p-6 shadow-md"
        >
          <div>
            <Label htmlFor="email">Email</Label>
            <Input
              id="email"
              type="email"
              placeholder="you@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="email"
              required
            />
          </div>
          <div>
            <Label htmlFor="password">Пароль</Label>
            <Input
              id="password"
              type="password"
              placeholder="••••••••"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              required
            />
          </div>
          {error && (
            <p className="rounded-lg bg-danger-weak px-3 py-2 text-sm font-medium text-danger">
              {error}
            </p>
          )}
          <Button type="submit" size="lg" block loading={loading}>
            <LogIn size={17} /> Войти
          </Button>
        </form>

        <p className="mt-5 text-center text-sm text-fg-muted">
          Нет аккаунта?{' '}
          <a href="/register" className="font-medium text-link hover:underline">
            Зарегистрироваться
          </a>
        </p>
      </div>
    </div>
  );
}
