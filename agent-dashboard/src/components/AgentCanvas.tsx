import { useCallback, useMemo } from 'react';
import ReactFlow, {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  Node,
  Edge,
  useNodesState,
  useEdgesState,
} from 'reactflow';
import 'reactflow/dist/style.css';
import { useDashboardStore } from '../store';
import { AgentNode } from './AgentNode';

const nodeTypes = { agentNode: AgentNode };

// Layout: concentric rings
// Center: Orchestrator
// Inner ring: Reliability Squad (4)
// Middle ring: Fix Squad (4)
// Outer ring: Quality Squad (4) + Ops Squad (3)

const LAYOUT: Record<string, { x: number; y: number }> = {
  'orchestrator':       { x: 380, y: 240 },
  // Reliability Squad - inner ring
  'security-auditor':   { x: 160, y: 80 },
  'backend-reliability':{ x: 400, y: 40 },
  'bot-integration':    { x: 620, y: 80 },
  'frontend-qa':        { x: 700, y: 260 },
  // Fix Squad - middle ring
  'fix-backend':        { x: 600, y: 420 },
  'fix-frontend':       { x: 380, y: 460 },
  'fix-bot':            { x: 160, y: 420 },
  'fix-infra':          { x: 50, y: 260 },
  // Quality Squad
  'code-reviewer':      { x: 50, y: 100 },
  'accessibility':      { x: 200, y: -40 },
  'seo-specialist':     { x: 550, y: -40 },
  'performance':        { x: 750, y: 100 },
  // Ops Squad
  'devops':             { x: 750, y: 400 },
  'monitoring':         { x: 550, y: 520 },
  'db-architect':       { x: 200, y: 520 },
};

export function AgentCanvas() {
  const { agents, selectedAgentId, setSelectedAgent } = useDashboardStore();

  const initialNodes: Node[] = useMemo(() => agents.map(agent => ({
    id: agent.id,
    type: 'agentNode',
    position: LAYOUT[agent.id] || { x: 0, y: 0 },
    data: {
      ...agent,
      isSelected: agent.id === selectedAgentId,
      onClick: setSelectedAgent,
    },
    draggable: true,
  })), [agents, selectedAgentId, setSelectedAgent]);

  const [nodes, , onNodesChange] = useNodesState(initialNodes);
  const [edges, , onEdgesChange] = useEdgesState([]);

  // Sync nodes when agents change
  const syncedNodes = useMemo(() => nodes.map(n => {
    const agent = agents.find(a => a.id === n.id);
    if (!agent) return n;
    return {
      ...n,
      data: {
        ...agent,
        isSelected: agent.id === selectedAgentId,
        onClick: setSelectedAgent,
      },
    };
  }), [nodes, agents, selectedAgentId, setSelectedAgent]);

  // Create animated edges from orchestrator to active agents
  const dynamicEdges: Edge[] = useMemo(() => {
    const activeAgents = agents.filter(a => a.status !== 'idle' && a.id !== 'orchestrator');
    return activeAgents.map(agent => ({
      id: `orchestrator-${agent.id}`,
      source: 'orchestrator',
      target: agent.id,
      animated: true,
      style: {
        stroke: agent.status === 'completed' ? '#22c55e' :
                agent.status === 'error' ? '#ef4444' :
                agent.status === 'thinking' ? '#C9A84C' : '#229ED9',
        strokeWidth: 1.5,
        opacity: 0.7,
      },
    }));
  }, [agents]);

  const handleNodeClick = useCallback((_: unknown, node: Node) => {
    setSelectedAgent(node.id === selectedAgentId ? null : node.id);
  }, [selectedAgentId, setSelectedAgent]);

  const handlePaneClick = useCallback(() => {
    setSelectedAgent(null);
  }, [setSelectedAgent]);

  return (
    <div style={{ width: '100%', height: '100%', background: '#080808' }}>
      <ReactFlow
        nodes={syncedNodes}
        edges={dynamicEdges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={handleNodeClick}
        onPaneClick={handlePaneClick}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.15 }}
        minZoom={0.3}
        maxZoom={2}
        attributionPosition="bottom-left"
        proOptions={{ hideAttribution: true }}
      >
        <Background
          variant={BackgroundVariant.Dots}
          gap={24}
          size={1}
          color="#1a1a2e"
        />
        <Controls
          style={{
            background: '#0d0d1a',
            border: '1px solid #C9A84C44',
            borderRadius: 6,
          }}
        />
        <MiniMap
          style={{
            background: '#0d0d1a',
            border: '1px solid #C9A84C44',
          }}
          nodeColor={(node) => {
            const agent = agents.find(a => a.id === node.id);
            if (!agent) return '#4b5563';
            const colors = {
              idle: '#4b5563',
              thinking: '#C9A84C',
              working: '#229ED9',
              completed: '#22c55e',
              error: '#ef4444',
            };
            return colors[agent.status];
          }}
          maskColor="#08080899"
        />
      </ReactFlow>
    </div>
  );
}
