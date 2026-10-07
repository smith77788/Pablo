'use client';
import { cn } from './cn';

function scoreColor(v: number) {
  if (v >= 75) return 'var(--success)';
  if (v >= 50) return 'var(--warning)';
  return 'var(--danger)';
}

/** Inline score bar with optional numeric label. */
export function ScoreBar({
  value,
  label,
  className,
  width = 'w-16',
}: {
  value: number;
  label?: boolean;
  className?: string;
  width?: string;
}) {
  return (
    <div className={cn('flex items-center gap-2', className)}>
      <div className={cn('h-1.5 overflow-hidden rounded-full bg-surface-2', width)}>
        <div
          className="h-full rounded-full transition-all"
          style={{ width: `${Math.max(0, Math.min(100, value))}%`, background: scoreColor(value) }}
        />
      </div>
      {label && <span className="text-xs tabular-nums text-fg-muted">{value}</span>}
    </div>
  );
}

/** Circular score ring for hero metrics. */
export function ScoreRing({
  value,
  size = 56,
  stroke = 6,
  children,
}: {
  value: number;
  size?: number;
  stroke?: number;
  children?: React.ReactNode;
}) {
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const offset = c - (Math.max(0, Math.min(100, value)) / 100) * c;
  return (
    <div className="relative inline-flex items-center justify-center" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90">
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--surface-2)" strokeWidth={stroke} />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={scoreColor(value)}
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={c}
          strokeDashoffset={offset}
          style={{ transition: 'stroke-dashoffset 0.5s ease' }}
        />
      </svg>
      <span className="absolute text-xs font-bold tabular-nums text-fg">{children ?? value}</span>
    </div>
  );
}
