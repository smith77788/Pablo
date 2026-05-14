import { memo } from 'react';
import { Handle, Position, type NodeProps } from 'reactflow';
import { motion } from 'framer-motion';
import type { Agent, AgentStatus } from '../store';

const STATUS_COLOR: Record<AgentStatus, string> = {
  idle: '#4b5563',
  thinking: '#C9A84C',
  working: '#229ED9',
  completed: '#22c55e',
  error: '#ef4444',
};

const STATUS_BG: Record<AgentStatus, string> = {
  idle: '#1f2937',
  thinking: '#2a200a',
  working: '#0a1a2e',
  completed: '#052e16',
  error: '#2d0a0a',
};

const STATUS_LABEL: Record<AgentStatus, string> = {
  idle: 'IDLE',
  thinking: 'THINKING',
  working: 'WORKING',
  completed: 'DONE',
  error: 'ERROR',
};

interface AgentNodeData extends Agent {
  isSelected: boolean;
  onClick: (id: string) => void;
}

export const AgentNode = memo(({ data }: NodeProps<AgentNodeData>) => {
  const color = STATUS_COLOR[data.status];
  const bg = STATUS_BG[data.status];
  const isActive = data.status === 'working' || data.status === 'thinking';

  return (
    <>
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />
      <motion.div
        className={isActive ? (data.status === 'working' ? 'agent-working' : 'agent-thinking') : ''}
        onClick={() => data.onClick(data.id)}
        whileHover={{ scale: 1.05 }}
        whileTap={{ scale: 0.97 }}
        style={{
          background: bg,
          border: `1px solid ${data.isSelected ? '#C9A84C' : color}`,
          borderRadius: 8,
          padding: '8px 12px',
          minWidth: 140,
          maxWidth: 160,
          cursor: 'pointer',
          position: 'relative',
          overflow: 'hidden',
        }}
      >
        {/* Active glow overlay */}
        {isActive && (
          <motion.div
            style={{
              position: 'absolute',
              inset: 0,
              background: `radial-gradient(ellipse at center, ${color}15 0%, transparent 70%)`,
              borderRadius: 8,
            }}
            animate={{ opacity: [0.3, 0.8, 0.3] }}
            transition={{ duration: 2, repeat: Infinity }}
          />
        )}

        {/* Content */}
        <div style={{ position: 'relative', zIndex: 1 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
            <span style={{ fontSize: 18, lineHeight: 1 }}>{data.emoji}</span>
            <div style={{
              fontSize: 9,
              fontWeight: 700,
              padding: '1px 5px',
              borderRadius: 3,
              background: color + '33',
              color: color,
              letterSpacing: '0.05em',
            }}>
              {STATUS_LABEL[data.status]}
            </div>
          </div>

          <div style={{
            fontSize: 11,
            fontWeight: 600,
            color: data.isSelected ? '#C9A84C' : '#e5e7eb',
            lineHeight: 1.3,
            marginBottom: 4,
          }}>
            {data.name}
          </div>

          {data.lastMessage && (
            <div style={{
              fontSize: 9,
              color: '#6b7280',
              lineHeight: 1.3,
              overflow: 'hidden',
              display: '-webkit-box',
              WebkitLineClamp: 2,
              WebkitBoxOrient: 'vertical',
            }}>
              {data.lastMessage.slice(0, 60)}{data.lastMessage.length > 60 ? '…' : ''}
            </div>
          )}
        </div>

        {/* Active indicator dot */}
        <motion.div
          style={{
            position: 'absolute',
            top: 6,
            right: 6,
            width: 6,
            height: 6,
            borderRadius: '50%',
            background: color,
          }}
          animate={isActive ? { opacity: [1, 0.2, 1] } : { opacity: 0.5 }}
          transition={isActive ? { duration: 1, repeat: Infinity } : undefined}
        />
      </motion.div>
      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
    </>
  );
});

AgentNode.displayName = 'AgentNode';
