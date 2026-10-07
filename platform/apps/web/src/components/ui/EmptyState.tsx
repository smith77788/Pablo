'use client';
import { cn } from './cn';

export function EmptyState({
  icon,
  title,
  description,
  action,
  className,
}: {
  icon?: React.ReactNode;
  title: string;
  description?: string;
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        'flex flex-col items-center justify-center gap-3 rounded-2xl border border-dashed border-line-strong bg-surface px-6 py-14 text-center',
        className,
      )}
    >
      {icon && (
        <span className="flex h-14 w-14 items-center justify-center rounded-2xl bg-surface-2 text-fg-hint">
          {icon}
        </span>
      )}
      <div>
        <p className="font-semibold text-fg">{title}</p>
        {description && <p className="mx-auto mt-1 max-w-xs text-sm text-fg-muted">{description}</p>}
      </div>
      {action}
    </div>
  );
}
