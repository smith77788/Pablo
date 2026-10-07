'use client';
import { ArrowDownRight, ArrowUpRight } from 'lucide-react';
import { cn } from './cn';
import type { Tone } from './Badge';

const ICON_TONES: Record<Tone, string> = {
  neutral: 'bg-surface-2 text-fg-muted',
  accent: 'bg-accent-weak text-accent',
  success: 'bg-success-weak text-success',
  warning: 'bg-warning-weak text-warning',
  danger: 'bg-danger-weak text-danger',
  violet: 'bg-violet-weak text-violet',
};

export function Stat({
  label,
  value,
  icon,
  tone = 'accent',
  delta,
  deltaLabel,
  className,
}: {
  label: string;
  value: React.ReactNode;
  icon?: React.ReactNode;
  tone?: Tone;
  delta?: number;
  deltaLabel?: string;
  className?: string;
}) {
  const positive = (delta ?? 0) >= 0;
  return (
    <div
      className={cn(
        'rounded-2xl border border-line bg-surface p-4 shadow-sm flex flex-col gap-3',
        className,
      )}
    >
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-fg-muted">{label}</span>
        {icon && (
          <span
            className={cn('flex h-9 w-9 items-center justify-center rounded-xl', ICON_TONES[tone])}
          >
            {icon}
          </span>
        )}
      </div>
      <div>
        <p className="text-2xl font-bold tracking-tight text-fg tabular-nums">{value}</p>
        {delta !== undefined && (
          <p
            className={cn(
              'mt-1 inline-flex items-center gap-1 text-xs font-medium',
              positive ? 'text-success' : 'text-danger',
            )}
          >
            {positive ? <ArrowUpRight size={13} /> : <ArrowDownRight size={13} />}
            {positive ? '+' : ''}
            {typeof delta === 'number' ? delta.toLocaleString('ru') : delta}
            {deltaLabel && <span className="text-fg-hint font-normal">{deltaLabel}</span>}
          </p>
        )}
      </div>
    </div>
  );
}
