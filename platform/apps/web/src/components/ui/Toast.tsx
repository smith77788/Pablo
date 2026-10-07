'use client';
import { createContext, useCallback, useContext, useState } from 'react';
import { CheckCircle2, Info, AlertTriangle, XCircle } from 'lucide-react';
import { cn } from './cn';
import { haptic } from '@/lib/telegram';

type ToastKind = 'success' | 'error' | 'info' | 'warning';
interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
}

const ToastCtx = createContext<(message: string, kind?: ToastKind) => void>(() => {});

export function useToast() {
  return useContext(ToastCtx);
}

const ICONS: Record<ToastKind, React.ReactNode> = {
  success: <CheckCircle2 size={18} className="text-success" />,
  error: <XCircle size={18} className="text-danger" />,
  info: <Info size={18} className="text-accent" />,
  warning: <AlertTriangle size={18} className="text-warning" />,
};

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);

  const push = useCallback((message: string, kind: ToastKind = 'info') => {
    const id = Date.now() + Math.random();
    haptic(kind === 'error' ? 'error' : kind === 'success' ? 'success' : 'light');
    setItems((p) => [...p, { id, kind, message }]);
    setTimeout(() => setItems((p) => p.filter((t) => t.id !== id)), 3200);
  }, []);

  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="pointer-events-none fixed inset-x-0 top-3 z-[60] flex flex-col items-center gap-2 px-4 pt-safe">
        {items.map((t) => (
          <div
            key={t.id}
            className={cn(
              'pointer-events-auto flex w-full max-w-sm items-center gap-3 rounded-2xl border border-line',
              'bg-surface px-4 py-3 shadow-lg animate-pop-in',
            )}
          >
            {ICONS[t.kind]}
            <span className="text-sm font-medium text-fg">{t.message}</span>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}
