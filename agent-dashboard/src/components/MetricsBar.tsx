import { motion } from 'framer-motion';
import { useDashboardStore } from '../store';

function formatTime(dt: string | null): string {
  if (!dt) return '—';
  try {
    const d = new Date(dt);
    return d.toLocaleTimeString('uk-UA', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch {
    return dt;
  }
}

export function MetricsBar() {
  const { agents, totalLogs, lastFetch } = useDashboardStore();
  const activeCount = agents.filter(a => a.status !== 'idle').length;
  const workingCount = agents.filter(a => a.status === 'working').length;
  const errorCount = agents.filter(a => a.status === 'error').length;

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 20,
      padding: '0 16px',
      height: 40,
      background: '#0d0d1a',
      borderBottom: '1px solid #C9A84C33',
      flexShrink: 0,
    }}>
      {/* Title */}
      <div style={{
        fontSize: 11,
        fontWeight: 700,
        color: '#C9A84C',
        letterSpacing: '0.15em',
        marginRight: 10,
        whiteSpace: 'nowrap',
      }}>
        NEVESTY MODELS — AGENT CONTROL CENTER
      </div>

      <div style={{ flex: 1 }} />

      {/* Metrics */}
      <Metric label="LOG ENTRIES" value={totalLogs} color="#9ca3af" />
      <Metric label="ACTIVE" value={activeCount} color="#229ED9" />
      <Metric label="WORKING" value={workingCount} color="#C9A84C" pulse={workingCount > 0} />
      <Metric label="ERRORS" value={errorCount} color="#ef4444" />

      <div style={{ width: 1, height: 20, background: '#1f2937' }} />

      <div style={{ fontSize: 10, color: '#374151', whiteSpace: 'nowrap' }}>
        <span style={{ color: '#4b5563' }}>LAST: </span>
        <span style={{ color: '#6b7280' }}>{formatTime(lastFetch)}</span>
      </div>

      {/* Live indicator */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
        <motion.div
          style={{
            width: 6,
            height: 6,
            borderRadius: '50%',
            background: '#22c55e',
          }}
          animate={{ opacity: [1, 0.2, 1] }}
          transition={{ duration: 2, repeat: Infinity }}
        />
        <span style={{ fontSize: 9, color: '#22c55e', letterSpacing: '0.08em' }}>LIVE</span>
      </div>
    </div>
  );
}

interface MetricProps {
  label: string;
  value: number;
  color: string;
  pulse?: boolean;
}

function Metric({ label, value, color, pulse }: MetricProps) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
      <span style={{ fontSize: 9, color: '#374151', letterSpacing: '0.06em' }}>{label}</span>
      <motion.span
        key={value}
        initial={{ scale: 1.3 }}
        animate={{ scale: 1 }}
        style={{ fontSize: 13, fontWeight: 700, color }}
      >
        {value}
      </motion.span>
      {pulse && value > 0 && (
        <motion.div
          style={{ width: 4, height: 4, borderRadius: '50%', background: color }}
          animate={{ opacity: [1, 0, 1] }}
          transition={{ duration: 1, repeat: Infinity }}
        />
      )}
    </div>
  );
}
