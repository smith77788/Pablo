'use client';
import { forwardRef } from 'react';
import { Loader2 } from 'lucide-react';
import { cn } from './cn';
import { haptic } from '@/lib/telegram';

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'success';
type Size = 'sm' | 'md' | 'lg' | 'icon';

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
  block?: boolean;
}

const VARIANTS: Record<Variant, string> = {
  primary:
    'bg-accent text-accent-fg hover:brightness-110 active:brightness-95 shadow-sm disabled:opacity-50',
  secondary:
    'bg-surface-2 text-fg hover:bg-line border border-line disabled:opacity-50',
  ghost: 'text-fg-muted hover:bg-surface-2 hover:text-fg disabled:opacity-40',
  danger: 'bg-danger text-white hover:brightness-110 active:brightness-95 disabled:opacity-50',
  success: 'bg-success text-white hover:brightness-110 active:brightness-95 disabled:opacity-50',
};

const SIZES: Record<Size, string> = {
  sm: 'h-8 px-3 text-xs gap-1.5 rounded-lg',
  md: 'h-10 px-4 text-sm gap-2 rounded-xl',
  lg: 'h-12 px-5 text-[15px] gap-2 rounded-2xl',
  icon: 'h-9 w-9 justify-center rounded-xl',
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'primary', size = 'md', loading, block, className, children, onClick, disabled, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      disabled={disabled || loading}
      onClick={(e) => {
        haptic(variant === 'danger' ? 'warning' : 'light');
        onClick?.(e);
      }}
      className={cn(
        'inline-flex items-center font-medium transition-all duration-150 select-none',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)]',
        'disabled:cursor-not-allowed active:scale-[0.98]',
        VARIANTS[variant],
        SIZES[size],
        block && 'w-full justify-center',
        className,
      )}
      {...rest}
    >
      {loading && <Loader2 size={size === 'sm' ? 14 : 16} className="animate-spin" />}
      {children}
    </button>
  );
});
