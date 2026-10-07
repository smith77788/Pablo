'use client';
import { cn } from './cn';
import { haptic } from '@/lib/telegram';

export interface SegOption<T extends string> {
  value: T;
  label: string;
  count?: number;
}

export function Segmented<T extends string>({
  options,
  value,
  onChange,
  className,
}: {
  options: SegOption<T>[];
  value: T;
  onChange: (v: T) => void;
  className?: string;
}) {
  return (
    <div
      className={cn(
        'inline-flex items-center gap-1 rounded-xl bg-surface-2 p-1 overflow-x-auto no-scrollbar max-w-full',
        className,
      )}
    >
      {options.map((o) => {
        const active = o.value === value;
        return (
          <button
            key={o.value}
            onClick={() => {
              haptic('select');
              onChange(o.value);
            }}
            className={cn(
              'relative whitespace-nowrap rounded-lg px-3 py-1.5 text-xs font-medium transition-all',
              active ? 'bg-surface text-fg shadow-sm' : 'text-fg-muted hover:text-fg',
            )}
          >
            {o.label}
            {o.count !== undefined && (
              <span className={cn('ml-1.5', active ? 'text-accent' : 'text-fg-hint')}>
                {o.count}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}
