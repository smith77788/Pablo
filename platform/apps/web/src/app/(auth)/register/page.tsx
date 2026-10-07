'use client';
import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { UserPlus } from 'lucide-react';
import { api } from '@/lib/api';
import { Button, Input, Label } from '@/components/ui';

export default function RegisterPage() {
  const router = useRouter();
  const [form, setForm] = useState({ tenantName: '', email: '', password: '' });
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError('');
    try {
      const data = await api.post('/auth/register', form);
      localStorage.setItem('token', data.accessToken);
      localStorage.setItem('refreshToken', data.refreshToken);
      router.push('/dashboard');
    } catch (err: any) {
      setError(err?.response?.data?.message ?? 'Ошибка регистрации');
    } finally {
      setLoading(false);
    }
  }

  const set = (k: string) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));

  return (
    <div className="flex min-h-screen items-center justify-center bg-bg px-4 py-10">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex flex-col items-center text-center">
          <span className="mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-accent text-2xl font-bold text-accent-fg shadow-md">
            i
          </span>
          <h1 className="text-2xl font-bold tracking-tight text-fg">Создать аккаунт</h1>
          <p className="mt-1 text-sm text-fg-muted">Начните управлять инфраструктурой за минуту</p>
        </div>

        <form
          onSubmit={submit}
          className="space-y-4 rounded-3xl border border-line bg-surface p-6 shadow-md"
        >
          <div>
            <Label htmlFor="tenant">Название компании</Label>
            <Input id="tenant" placeholder="Моя команда" value={form.tenantName} onChange={set('tenantName')} required />
          </div>
          <div>
            <Label htmlFor="email">Email</Label>
            <Input id="email" type="email" placeholder="you@example.com" value={form.email} onChange={set('email')} required />
          </div>
          <div>
            <Label htmlFor="password">Пароль</Label>
            <Input
              id="password"
              type="password"
              placeholder="Минимум 8 символов"
              value={form.password}
              onChange={set('password')}
              required
              minLength={8}
            />
          </div>
          {error && (
            <p className="rounded-lg bg-danger-weak px-3 py-2 text-sm font-medium text-danger">{error}</p>
          )}
          <Button type="submit" size="lg" block loading={loading}>
            <UserPlus size={17} /> Создать аккаунт
          </Button>
        </form>

        <p className="mt-5 text-center text-sm text-fg-muted">
          Уже есть аккаунт?{' '}
          <a href="/login" className="font-medium text-link hover:underline">
            Войти
          </a>
        </p>
      </div>
    </div>
  );
}
