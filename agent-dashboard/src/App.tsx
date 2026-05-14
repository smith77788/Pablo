import { useEffect } from 'react';
import { ReactFlowProvider } from 'reactflow';
import { useDashboardStore } from './store';
import { AgentCanvas } from './components/AgentCanvas';
import { EventFeed } from './components/EventFeed';
import { AgentDetails } from './components/AgentDetails';
import { MetricsBar } from './components/MetricsBar';

const API_BASE = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1'
  ? 'http://localhost:3000'
  : '';

async function fetchLogs() {
  const res = await fetch(`${API_BASE}/api/agent-logs?limit=100`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export default function App() {
  const { agents, logs, selectedAgentId, updateFromLogs, tickIdle } = useDashboardStore();

  const selectedAgent = agents.find(a => a.id === selectedAgentId) || null;

  // Poll every 3 seconds
  useEffect(() => {
    let cancelled = false;

    async function poll() {
      try {
        const data = await fetchLogs();
        if (!cancelled) updateFromLogs(data);
      } catch (e) {
        console.warn('Agent log fetch error:', e);
      }
    }

    poll();
    const interval = setInterval(poll, 3000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [updateFromLogs]);

  // Tick idle state every 5 seconds
  useEffect(() => {
    const interval = setInterval(tickIdle, 5000);
    return () => clearInterval(interval);
  }, [tickIdle]);

  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      height: '100vh',
      background: '#080808',
      fontFamily: "'JetBrains Mono', 'Fira Code', 'Courier New', monospace",
      overflow: 'hidden',
    }}>
      {/* Top metrics bar */}
      <MetricsBar />

      {/* Main content */}
      <div style={{ flex: 1, display: 'flex', overflow: 'hidden' }}>
        {/* Left: Agent canvas */}
        <div style={{ flex: 1, position: 'relative', overflow: 'hidden' }}>
          <ReactFlowProvider>
            <AgentCanvas />
          </ReactFlowProvider>
        </div>

        {/* Right panel */}
        <div style={{
          width: 300,
          display: 'flex',
          flexDirection: 'column',
          background: '#0a0a0a',
          borderLeft: '1px solid #C9A84C22',
          overflow: 'hidden',
        }}>
          {/* Event Feed */}
          <div style={{
            flex: 1,
            display: 'flex',
            flexDirection: 'column',
            overflow: 'hidden',
            borderBottom: '1px solid #1f2937',
          }}>
            <div style={{
              padding: '8px 12px',
              fontSize: 9,
              color: '#C9A84C',
              letterSpacing: '0.12em',
              fontWeight: 700,
              background: '#0d0d1a',
              borderBottom: '1px solid #1f293733',
              flexShrink: 0,
            }}>
              EVENT FEED
            </div>
            <EventFeed logs={logs} />
          </div>

          {/* Agent Details */}
          <div style={{
            height: 220,
            flexShrink: 0,
            overflow: 'auto',
          }}>
            <div style={{
              padding: '8px 12px',
              fontSize: 9,
              color: '#C9A84C',
              letterSpacing: '0.12em',
              fontWeight: 700,
              background: '#0d0d1a',
              borderBottom: '1px solid #1f293733',
            }}>
              AGENT DETAILS
            </div>
            <AgentDetails agent={selectedAgent} />
          </div>
        </div>
      </div>
    </div>
  );
}
