import { create } from 'zustand';

export type AgentStatus = 'idle' | 'thinking' | 'working' | 'completed' | 'error';

export interface AgentLog {
  id: number;
  from_name: string;
  message: string;
  created_at: string;
}

export interface Agent {
  id: string;
  name: string;
  emoji: string;
  role: 'orchestrator' | 'reliability' | 'fix' | 'quality' | 'ops';
  status: AgentStatus;
  lastMessage: string;
  lastActive: string | null;
}

const AGENTS: Agent[] = [
  { id: 'orchestrator', name: 'Orchestrator', emoji: '🎯', role: 'orchestrator', status: 'idle', lastMessage: '', lastActive: null },
  { id: 'security-auditor', name: 'Security Auditor', emoji: '🛡️', role: 'reliability', status: 'idle', lastMessage: '', lastActive: null },
  { id: 'backend-reliability', name: 'Backend Reliability', emoji: '⚙️', role: 'reliability', status: 'idle', lastMessage: '', lastActive: null },
  { id: 'bot-integration', name: 'Bot Integration', emoji: '🤖', role: 'reliability', status: 'idle', lastMessage: '', lastActive: null },
  { id: 'frontend-qa', name: 'Frontend QA', emoji: '🖥️', role: 'reliability', status: 'idle', lastMessage: '', lastActive: null },
  { id: 'fix-backend', name: 'Fix-Backend Engineer', emoji: '🔧', role: 'fix', status: 'idle', lastMessage: '', lastActive: null },
  { id: 'fix-frontend', name: 'Fix-Frontend Engineer', emoji: '🎨', role: 'fix', status: 'idle', lastMessage: '', lastActive: null },
  { id: 'fix-bot', name: 'Fix-Bot Engineer', emoji: '🔩', role: 'fix', status: 'idle', lastMessage: '', lastActive: null },
  { id: 'fix-infra', name: 'Fix-Infra Engineer', emoji: '🏗️', role: 'fix', status: 'idle', lastMessage: '', lastActive: null },
  { id: 'code-reviewer', name: 'Code Reviewer', emoji: '📐', role: 'quality', status: 'idle', lastMessage: '', lastActive: null },
  { id: 'accessibility', name: 'Accessibility Auditor', emoji: '♿', role: 'quality', status: 'idle', lastMessage: '', lastActive: null },
  { id: 'seo-specialist', name: 'SEO Specialist', emoji: '🔍', role: 'quality', status: 'idle', lastMessage: '', lastActive: null },
  { id: 'performance', name: 'Performance Engineer', emoji: '⚡', role: 'quality', status: 'idle', lastMessage: '', lastActive: null },
  { id: 'devops', name: 'DevOps Engineer', emoji: '🚀', role: 'ops', status: 'idle', lastMessage: '', lastActive: null },
  { id: 'monitoring', name: 'Monitoring Engineer', emoji: '📊', role: 'ops', status: 'idle', lastMessage: '', lastActive: null },
  { id: 'db-architect', name: 'DB Architect', emoji: '🗄️', role: 'ops', status: 'idle', lastMessage: '', lastActive: null },
];

// Map from_name variants to agent IDs
function resolveAgentId(fromName: string): string | null {
  const n = fromName.toLowerCase();
  if (n.includes('orchestrat') || n === 'claude') return 'orchestrator';
  if (n.includes('security')) return 'security-auditor';
  if (n.includes('backend reliability') || (n.includes('backend') && n.includes('reliab'))) return 'backend-reliability';
  if (n.includes('bot integration') || n.includes('bot integr')) return 'bot-integration';
  if (n.includes('frontend qa') || n.includes('front') && n.includes('qa')) return 'frontend-qa';
  if (n.includes('fix-backend') || n.includes('fix backend')) return 'fix-backend';
  if (n.includes('fix-frontend') || n.includes('fix frontend')) return 'fix-frontend';
  if (n.includes('fix-bot') || n.includes('fix bot')) return 'fix-bot';
  if (n.includes('fix-infra') || n.includes('fix infra') || n.includes('infra')) return 'fix-infra';
  if (n.includes('code review')) return 'code-reviewer';
  if (n.includes('access')) return 'accessibility';
  if (n.includes('seo')) return 'seo-specialist';
  if (n.includes('performance')) return 'performance';
  if (n.includes('devops') || n.includes('dev ops')) return 'devops';
  if (n.includes('monitor')) return 'monitoring';
  if (n.includes('db') || n.includes('database') || n.includes('architect')) return 'db-architect';
  // Claude is orchestrator
  if (n === 'claude') return 'orchestrator';
  return null;
}

function parseStatus(message: string): AgentStatus {
  const m = message.toLowerCase();
  if (m.includes('починаю') || m.includes('запускаю') || m.includes('start') || m.includes('working') || m.includes('fixing') || m.includes('виправляю')) return 'working';
  if (m.includes('аналізую') || m.includes('перевіряю') || m.includes('thinking') || m.includes('analyzing') || m.includes('checking')) return 'thinking';
  if (m.includes('готово') || m.includes('завершив') || m.includes('done') || m.includes('✅') || m.includes('completed') || m.includes('finished')) return 'completed';
  if (m.includes('помилка') || m.includes('error') || m.includes('❌') || m.includes('failed') || m.includes('fail')) return 'error';
  return 'working'; // default for any message = working
}

interface DashboardState {
  agents: Agent[];
  logs: AgentLog[];
  selectedAgentId: string | null;
  lastFetch: string | null;
  totalLogs: number;
  setSelectedAgent: (id: string | null) => void;
  updateFromLogs: (logs: AgentLog[]) => void;
  tickIdle: () => void;
}

export const useDashboardStore = create<DashboardState>((set, get) => ({
  agents: AGENTS,
  logs: [],
  selectedAgentId: null,
  lastFetch: null,
  totalLogs: 0,

  setSelectedAgent: (id) => set({ selectedAgentId: id }),

  updateFromLogs: (logs) => {
    const now = Date.now();
    const agentUpdates: Record<string, Partial<Agent>> = {};

    // Process logs newest first to get latest status per agent
    for (const log of logs) {
      const agentId = resolveAgentId(log.from_name);
      if (!agentId) continue;
      if (agentUpdates[agentId]) continue; // already got newest for this agent

      const logTime = new Date(log.created_at).getTime();
      const ageMs = now - logTime;

      let status: AgentStatus;
      if (ageMs > 30000) {
        status = 'idle';
      } else {
        status = parseStatus(log.message);
      }

      agentUpdates[agentId] = {
        status,
        lastMessage: log.message,
        lastActive: log.created_at,
      };
    }

    const agents = get().agents.map(a => ({
      ...a,
      ...(agentUpdates[a.id] || {}),
    }));

    // Last activity
    const lastFetch = logs.length > 0 ? logs[0].created_at : get().lastFetch;

    set({ agents, logs: logs.slice(0, 50), lastFetch, totalLogs: logs.length });
  },

  tickIdle: () => {
    const now = Date.now();
    const agents = get().agents.map(a => {
      if (a.lastActive && a.status !== 'idle') {
        const ageMs = now - new Date(a.lastActive).getTime();
        if (ageMs > 30000) return { ...a, status: 'idle' as AgentStatus };
      }
      return a;
    });
    set({ agents });
  },
}));

export { AGENTS, resolveAgentId };
