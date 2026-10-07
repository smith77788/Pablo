'use client';

/**
 * Telegram Mini App (WebApp) integration helpers.
 *
 * The dashboard doubles as a Telegram Mini App. When it runs inside Telegram
 * we adopt the native theme (light/dark + accent colors), wire the hardware
 * Back button, and expose haptics / main button helpers. When it runs as a
 * plain web page (no Telegram host) everything degrades gracefully to the
 * CSS fallback theme and no-op helpers.
 */

type ThemeParams = Record<string, string>;

interface TelegramWebApp {
  initData: string;
  initDataUnsafe: Record<string, unknown>;
  colorScheme: 'light' | 'dark';
  themeParams: ThemeParams;
  isExpanded: boolean;
  viewportHeight: number;
  viewportStableHeight: number;
  headerColor: string;
  backgroundColor: string;
  ready: () => void;
  expand: () => void;
  close: () => void;
  onEvent: (event: string, cb: () => void) => void;
  offEvent: (event: string, cb: () => void) => void;
  setHeaderColor?: (color: string) => void;
  setBackgroundColor?: (color: string) => void;
  BackButton: {
    isVisible: boolean;
    show: () => void;
    hide: () => void;
    onClick: (cb: () => void) => void;
    offClick: (cb: () => void) => void;
  };
  MainButton: {
    text: string;
    isVisible: boolean;
    show: () => void;
    hide: () => void;
    setText: (t: string) => void;
    onClick: (cb: () => void) => void;
    offClick: (cb: () => void) => void;
    showProgress: (leaveActive?: boolean) => void;
    hideProgress: () => void;
    setParams: (p: Record<string, unknown>) => void;
  };
  HapticFeedback?: {
    impactOccurred: (style: 'light' | 'medium' | 'heavy' | 'rigid' | 'soft') => void;
    notificationOccurred: (type: 'error' | 'success' | 'warning') => void;
    selectionChanged: () => void;
  };
}

declare global {
  interface Window {
    Telegram?: { WebApp?: TelegramWebApp };
  }
}

export function getTG(): TelegramWebApp | null {
  if (typeof window === 'undefined') return null;
  return window.Telegram?.WebApp ?? null;
}

export function isInTelegram(): boolean {
  const tg = getTG();
  return !!tg && !!tg.initData;
}

/** Map a Telegram themeParams object onto our semantic CSS variables. */
function applyThemeParams(tg: TelegramWebApp) {
  const root = document.documentElement;
  const tp = tg.themeParams ?? {};
  const set = (cssVar: string, value?: string) => {
    if (value) root.style.setProperty(cssVar, value);
  };

  // Base surfaces & text
  set('--bg', tp.bg_color);
  set('--surface', tp.section_bg_color ?? tp.bg_color);
  set('--surface-2', tp.secondary_bg_color);
  set('--fg', tp.text_color);
  set('--fg-muted', tp.subtitle_text_color ?? tp.hint_color);
  set('--hint', tp.hint_color);
  set('--link', tp.link_color);
  set('--accent', tp.button_color);
  set('--accent-fg', tp.button_text_color);
  set('--danger', tp.destructive_text_color);
  if (tp.hint_color) root.style.setProperty('--border', hexWithAlpha(tp.hint_color, 0.24));
  if (tp.button_color) root.style.setProperty('--accent-weak', hexWithAlpha(tp.button_color, 0.14));
  if (tp.button_color) root.style.setProperty('--ring', hexWithAlpha(tp.button_color, 0.4));

  // Color scheme toggles the dark token set for anything not overridden above
  root.classList.toggle('dark', tg.colorScheme === 'dark');
  root.setAttribute('data-theme', tg.colorScheme);

  // Match the Telegram chrome to our page background
  try {
    tg.setBackgroundColor?.(tp.bg_color ?? (tg.colorScheme === 'dark' ? '#17212b' : '#ffffff'));
    tg.setHeaderColor?.(tp.secondary_bg_color ?? tp.bg_color ?? '#ffffff');
  } catch {
    /* older clients */
  }
}

function hexWithAlpha(hex: string, alpha: number): string {
  const m = hex.replace('#', '');
  if (m.length !== 6) return hex;
  const r = parseInt(m.slice(0, 2), 16);
  const g = parseInt(m.slice(2, 4), 16);
  const b = parseInt(m.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

/**
 * Initialize the Mini App. Returns a cleanup function.
 * Safe to call when not inside Telegram — it just records the OS color scheme.
 */
export function initTelegram(): () => void {
  const tg = getTG();
  if (!tg) {
    // Standalone web: honor OS dark mode via the media-query fallback in CSS.
    return () => {};
  }

  tg.ready();
  try {
    tg.expand();
  } catch {
    /* noop */
  }
  applyThemeParams(tg);

  const onTheme = () => applyThemeParams(tg);
  tg.onEvent('themeChanged', onTheme);

  return () => {
    tg.offEvent('themeChanged', onTheme);
  };
}

export function haptic(
  kind: 'light' | 'medium' | 'heavy' | 'success' | 'warning' | 'error' | 'select' = 'light',
) {
  const tg = getTG();
  if (!tg?.HapticFeedback) return;
  try {
    if (kind === 'success' || kind === 'warning' || kind === 'error') {
      tg.HapticFeedback.notificationOccurred(kind);
    } else if (kind === 'select') {
      tg.HapticFeedback.selectionChanged();
    } else {
      tg.HapticFeedback.impactOccurred(kind);
    }
  } catch {
    /* noop */
  }
}

/** Show/hide and bind the native Back button. Returns cleanup. */
export function bindBackButton(visible: boolean, onBack: () => void): () => void {
  const tg = getTG();
  if (!tg) return () => {};
  const { BackButton } = tg;
  if (visible) {
    BackButton.onClick(onBack);
    BackButton.show();
  } else {
    BackButton.hide();
  }
  return () => {
    try {
      BackButton.offClick(onBack);
      BackButton.hide();
    } catch {
      /* noop */
    }
  };
}
