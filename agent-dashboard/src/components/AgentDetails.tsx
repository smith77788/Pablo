import { motion } from 'framer-motion';
import { Agent, AgentStatus } from '../store';

const STATUS_COLOR: Record<AgentStatus, string> = {
  idle: '#4b5563',
  thinking: '#C9A84C',
  working: '#229ED9',
  completed: '#22c55e',
  error: '#ef4444',
};

const ROLE_LABEL: Record<string, string> = {
  orchestrator: 'ORCHESTRATOR',
  reliability: 'RELIABILITY SQUAD',
  fix: 'FIX SQUAD',
  quality: 'QUALITY SQUAD',
  ops: 'OPS SQUAD',
};

function formatTime(dt: string | null): string {
  if (!dt) return 'never';
  try {
    const d = new Date(dt);
    return d.toLocaleTimeString('uk-UA', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch {
    return dt;
  }
}

interface AgentDetailsProps {
  agent: Agent | null;
}

export function AgentDetails({ agent }: AgentDetailsProps) {
  if (!agent) {
    return (
      <div style={{
        padding: '16px 12px',
        color: '#374151',
        fontSize: 11,
        textAlign: 'center',
      }}>
        Click an agent node to inspect
      </div>
    );
  }

  const color = STATUS_COLOR[agent.status];

  return (
    <motion.div
      key={agent.id}
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      style={{ padding: '12px' }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
        <span style={{ fontSize: 28 }}>{agent.emoji}</span>
        <div>
          <div style={{ fontSize: 13, fontWeight: 700, color: '#e5e7eb' }}>{agent.name}</div>
          <div style={{ fontSize: 9, color: '#6b7280', letterSpacing: '0.08em' }}>
            {ROLE_LABEL[agent.role]}
          </div>
        </div>
      </div>

      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: 6,
        marginBottom: 10,
        padding: '6px 10px',
        background: color + '15',
        border: `1px solid ${color}44`,
        borderRadius: 6,
      }}>
        <motion.div
          style={{
            width: 8,
            height: 8,
            borderRadius: '50%',
            background: color,
            flexShrink: 0,
          }}
          animate={agent.status !== 'idle' ? { opacity: [1, 0.3, 1] } : {}}
          transition={{ duration: 1.5, repeat: Infinity }}
        />
        <span style={{ fontSize: 11, color, fontWeight: 700, letterSpacing: '0.05em' }}>
          {agent.status.toUpperCase()}
        </span>
      </div>

      <div style={{ marginBottom: 8 }}>
        <div style={{ fontSize: 9, color: '#4b5563', marginBottom: 4, letterSpacing: '0.06em' }}>
          LAST ACTIVITY
        </div>
        <div style={{ fontSize: 10, color: '#6b7280' }}>
          {formatTime(agent.lastActive)}
        </div>
      </div>

      {agent.lastMessage && (
        <div>
          <div style={{ fontSize: 9, color: '#4b5563', marginBottom: 4, letterSpacing: '0.06em' }}>
            LAST MESSAGE
          </div>
          <div style={{
            fontSize: 10,
            color: '#9ca3af',
            background: '#0d0d1a',
            border: '1px solid #1f2937',
            borderRadius: 4,
            padding: '8px',
            lineHeight: 1.6,
            wordBreak: 'break-word',
          }}>
            {agent.lastMessage}
          </div>
        </div>
      )}
    </motion.div>
  );
}
