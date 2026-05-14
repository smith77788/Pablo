import { useEffect, useRef } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { AgentLog } from '../store';

const ROLE_COLOR: Record<string, string> = {
  orchestrator: '#C9A84C',
  reliability: '#229ED9',
  fix: '#f59e0b',
  quality: '#a78bfa',
  ops: '#34d399',
};

function getRoleColor(fromName: string): string {
  const n = fromName.toLowerCase();
  if (n === 'claude' || n.includes('orchestrat')) return ROLE_COLOR.orchestrator;
  if (n.includes('security') || n.includes('backend reliab') || n.includes('bot integr') || n.includes('frontend qa')) return ROLE_COLOR.reliability;
  if (n.includes('fix')) return ROLE_COLOR.fix;
  if (n.includes('code review') || n.includes('access') || n.includes('seo') || n.includes('performance')) return ROLE_COLOR.quality;
  if (n.includes('devops') || n.includes('monitor') || n.includes('db') || n.includes('architect')) return ROLE_COLOR.ops;
  return '#9ca3af';
}

function formatTime(dt: string): string {
  try {
    const d = new Date(dt);
    return d.toLocaleTimeString('uk-UA', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  } catch {
    return dt;
  }
}

interface EventFeedProps {
  logs: AgentLog[];
}

export function EventFeed({ logs }: EventFeedProps) {
  const containerRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to top when new logs arrive (newest on top)
  useEffect(() => {
    if (containerRef.current) {
      containerRef.current.scrollTop = 0;
    }
  }, [logs.length]);

  if (logs.length === 0) {
    return (
      <div style={{
        flex: 1,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        color: '#4b5563',
        fontSize: 12,
        fontFamily: 'monospace',
      }}>
        Waiting for agent activity…
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      style={{
        flex: 1,
        overflowY: 'auto',
        padding: '8px 0',
      }}
    >
      <AnimatePresence initial={false}>
        {logs.map((log) => (
          <motion.div
            key={log.id}
            initial={{ opacity: 0, x: 20 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: 0.2 }}
            style={{
              padding: '8px 12px',
              borderBottom: '1px solid #0d0d1a',
              fontSize: 11,
              lineHeight: 1.5,
            }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 2 }}>
              <span style={{
                color: getRoleColor(log.from_name),
                fontWeight: 700,
                fontSize: 10,
                letterSpacing: '0.04em',
              }}>
                {log.from_name.replace('Agent: ', '')}
              </span>
              <span style={{ color: '#374151', fontSize: 9 }}>
                {formatTime(log.created_at)}
              </span>
            </div>
            <div style={{ color: '#9ca3af', wordBreak: 'break-word' }}>
              {log.message}
            </div>
          </motion.div>
        ))}
      </AnimatePresence>
    </div>
  );
}
