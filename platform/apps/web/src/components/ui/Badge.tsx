'use client';
import { cn } from './cn';

export type Tone = 'neutral' | 'accent' | 'success' | 'warning' | 'danger' | 'violet';

const TONES: Record<Tone, string> = {
  neutral: 'bg-surface-2 text-fg-muted',
  accent: 'bg-accent-weak text-accent',
  success: 'bg-success-weak text-success',
  warning: 'bg-warning-weak text-warning',
  danger: 'bg-danger-weak text-danger',
  violet: 'bg-violet-weak text-violet',
};

export function Badge({
  tone = 'neutral',
  dot,
  className,
  children,
}: {
  tone?: Tone;
  dot?: boolean;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-2xs font-semibold whitespace-nowrap',
        TONES[tone],
        className,
      )}
    >
      {dot && <span className="h-1.5 w-1.5 rounded-full bg-current" />}
      {children}
    </span>
  );
}

/** Maps common backend statuses to a tone + Russian label. */
export function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { tone: Tone; label: string }> = {
    ACTIVE: { tone: 'success', label: 'Активен' },
    RUNNING: { tone: 'success', label: 'Выполняется' },
    COMPLETED: { tone: 'success', label: 'Завершено' },
    WARNING: { tone: 'warning', label: 'Внимание' },
    PENDING: { tone: 'warning', label: 'Ожидает' },
    QUEUED: { tone: 'warning', label: 'В очереди' },
    LIMITED: { tone: 'danger', label: 'Ограничен' },
    FAILED: { tone: 'danger', label: 'Ошибка' },
    DISCONNECTED: { tone: 'neutral', label: 'Отключён' },
    ARCHIVED: { tone: 'neutral', label: 'В архиве' },
    PAUSED: { tone: 'neutral', label: 'Пауза' },
  };
  const m = map[status] ?? { tone: 'neutral' as Tone, label: status };
  return (
    <Badge tone={m.tone} dot>
      {m.label}
    </Badge>
  );
}
