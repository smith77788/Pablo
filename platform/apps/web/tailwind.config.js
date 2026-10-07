/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: ['class', '[data-theme="dark"]'],
  content: ['./src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: 'var(--bg)',
        surface: {
          DEFAULT: 'var(--surface)',
          2: 'var(--surface-2)',
        },
        fg: {
          DEFAULT: 'var(--fg)',
          muted: 'var(--fg-muted)',
          hint: 'var(--hint)',
        },
        line: {
          DEFAULT: 'var(--border)',
          strong: 'var(--border-strong)',
        },
        accent: {
          DEFAULT: 'var(--accent)',
          fg: 'var(--accent-fg)',
          weak: 'var(--accent-weak)',
        },
        link: 'var(--link)',
        success: { DEFAULT: 'var(--success)', weak: 'var(--success-weak)' },
        warning: { DEFAULT: 'var(--warning)', weak: 'var(--warning-weak)' },
        danger: { DEFAULT: 'var(--danger)', weak: 'var(--danger-weak)' },
        violet: { DEFAULT: 'var(--violet)', weak: 'var(--violet-weak)' },
      },
      borderColor: {
        DEFAULT: 'var(--border)',
      },
      borderRadius: {
        xl: '14px',
        '2xl': '18px',
        '3xl': '24px',
      },
      boxShadow: {
        sm: 'var(--shadow-sm)',
        md: 'var(--shadow-md)',
        lg: 'var(--shadow-lg)',
      },
      ringColor: {
        DEFAULT: 'var(--ring)',
      },
      fontSize: {
        '2xs': ['0.6875rem', { lineHeight: '1rem' }],
      },
    },
  },
  plugins: [],
};
