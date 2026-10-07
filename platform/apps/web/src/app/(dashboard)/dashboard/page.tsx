'use client';
import { useQuery } from '@tanstack/react-query';
import Link from 'next/link';
import {
  Users,
  MessageSquare,
  GitBranch,
  MessageCircleReply,
  Clock,
  Server,
  Shield,
  Send,
  PlusCircle,
  Activity,
  ChevronRight,
} from 'lucide-react';
import { authApi } from '@/lib/api';
import { PageHeader, Stat, Card, Badge, SkeletonCards } from '@/components/ui';

interface StatsOverview {
  totalUsers: number;
  newToday: number;
  messagesSent: number;
  messagesReceived: number;
  activeFunnels: number;
  activeReplies: number;
}

const QUICK_ACTIONS = [
  { href: '/operations/new', label: 'Новая операция', icon: PlusCircle, tone: 'accent' as const },
  { href: '/broadcasts', label: 'Рассылка', icon: Send, tone: 'violet' as const },
  { href: '/telegram-accounts', label: 'Аккаунты', icon: Shield, tone: 'success' as const },
  { href: '/assets', label: 'Активы', icon: Server, tone: 'warning' as const },
];

const ACTIVITY = [
  { type: 'Аккаунт', detail: '@account_main прошёл прогрев', time: '2 мин', tone: 'success' as const },
  { type: 'Операция', detail: 'Mass Publish — 142/200 выполнено', time: '9 мин', tone: 'accent' as const },
  { type: 'Воронка', detail: '«Онбординг» — запущена для 38 человек', time: '14 мин', tone: 'violet' as const },
  { type: 'Внимание', detail: '@account_promo: flood-wait 3600s', time: '26 мин', tone: 'warning' as const },
  { type: 'Рассылка', detail: '«Акция» доставлена 1 204 получателям', time: '51 мин', tone: 'violet' as const },
];

export default function DashboardPage() {
  const { data, isLoading } = useQuery<StatsOverview>({
    queryKey: ['stats-overview'],
    queryFn: () => authApi.get('/stats/overview'),
    refetchInterval: 30_000,
  });

  const s: StatsOverview = data ?? {
    totalUsers: 0,
    newToday: 0,
    messagesSent: 0,
    messagesReceived: 0,
    activeFunnels: 0,
    activeReplies: 0,
  };

  return (
    <div className="mx-auto max-w-5xl space-y-6 p-4 sm:p-6">
      <PageHeader title="Обзор" subtitle="Состояние вашей Telegram-инфраструктуры" />

      {/* Metric cards */}
      {isLoading ? (
        <SkeletonCards count={4} className="grid-cols-2 lg:grid-cols-4" />
      ) : (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <Stat
            label="Пользователей"
            value={s.totalUsers.toLocaleString('ru')}
            icon={<Users size={17} />}
            tone="accent"
            delta={s.newToday}
            deltaLabel="сегодня"
          />
          <Stat
            label="Отправлено"
            value={s.messagesSent.toLocaleString('ru')}
            icon={<MessageSquare size={17} />}
            tone="violet"
          />
          <Stat
            label="Получено"
            value={s.messagesReceived.toLocaleString('ru')}
            icon={<MessageCircleReply size={17} />}
            tone="success"
          />
          <Stat
            label="Активных воронок"
            value={s.activeFunnels.toLocaleString('ru')}
            icon={<GitBranch size={17} />}
            tone="warning"
          />
        </div>
      )}

      {/* Quick actions */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {QUICK_ACTIONS.map((a) => (
          <Link key={a.href} href={a.href}>
            <Card interactive className="flex items-center gap-3 p-4">
              <span
                className="flex h-10 w-10 items-center justify-center rounded-xl"
                style={{
                  background: `var(--${a.tone}-weak)`,
                  color: `var(--${a.tone})`,
                }}
              >
                <a.icon size={19} />
              </span>
              <span className="text-sm font-semibold text-fg">{a.label}</span>
            </Card>
          </Link>
        ))}
      </div>

      {/* Activity feed */}
      <Card>
        <div className="flex items-center gap-2 border-b border-line px-4 py-3.5">
          <Activity size={16} className="text-fg-muted" />
          <h2 className="text-sm font-semibold text-fg">Последние события</h2>
          <Link
            href="/operations/history"
            className="ml-auto flex items-center gap-0.5 text-xs font-medium text-link hover:underline"
          >
            Вся история <ChevronRight size={13} />
          </Link>
        </div>
        <div className="divide-y divide-line">
          {ACTIVITY.map((e, i) => (
            <div key={i} className="flex items-center gap-3 px-4 py-3">
              <Badge tone={e.tone}>{e.type}</Badge>
              <span className="min-w-0 flex-1 truncate text-sm text-fg">{e.detail}</span>
              <span className="flex items-center gap-1 whitespace-nowrap text-xs text-fg-hint">
                <Clock size={11} /> {e.time}
              </span>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
