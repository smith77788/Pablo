/**
 * Pablo AI — Executive AI Operating System Dashboard
 *
 * Головна сторінка Pablo AI в адмін-панелі BASIC.FOOD.
 * Включає:
 * - Company Health Score
 * - Ранковий брифінг (Claude CEO)
 * - Черга підтвердження рішень (approval queue)
 * - Виконавчі агенти (CEO / CMO / CFO / COO)
 * - Журнал рішень
 */

import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { supabase } from "@/integrations/supabase/client";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {
  Brain, Sparkles, CheckCircle2, XCircle, Clock, TrendingUp,
  AlertTriangle, Loader2, MessageSquare, Shield, RefreshCw,
  BarChart3, Package, Megaphone, DollarSign, Truck,
} from "lucide-react";
import { toast } from "sonner";
import { formatDistanceToNow } from "date-fns";
import { uk } from "date-fns/locale";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

// ─── Types ────────────────────────────────────────────────────────────────

type AgentRole = "ceo" | "cmo" | "cfo" | "coo" | "analyst" | "cos";

interface Decision {
  id: string;
  agent: AgentRole;
  decision_type: string;
  title: string;
  summary: string;
  reasoning: string;
  risk_level: string;
  approval_status: string;
  metrics_snapshot: Record<string, unknown>;
  created_at: string;
}

interface ApprovalItem {
  id: string;
  decision_id: string;
  agent: AgentRole;
  action_type: string;
  title: string;
  description: string;
  risk_level: string;
  payload: Record<string, unknown>;
  status: string;
  expires_at: string;
  created_at: string;
}

interface Briefing {
  id: string;
  title: string;
  content: string;
  metrics: Record<string, unknown>;
  sent_to_tg: boolean;
  created_at: string;
}

// ─── Agent config ─────────────────────────────────────────────────────────

const AGENTS: { role: AgentRole; label: string; icon: typeof Brain; color: string; description: string }[] = [
  { role: "ceo", label: "CEO", icon: Brain, color: "from-purple-500/20 to-purple-500/5", description: "Стратегія та пріоритети" },
  { role: "cmo", label: "CMO", icon: Megaphone, color: "from-blue-500/20 to-blue-500/5", description: "Маркетинг та кампанії" },
  { role: "cfo", label: "CFO", icon: DollarSign, color: "from-emerald-500/20 to-emerald-500/5", description: "Фінанси та unit economics" },
  { role: "coo", label: "COO", icon: Truck, color: "from-orange-500/20 to-orange-500/5", description: "Операції та логістика" },
  { role: "analyst", label: "Аналітик", icon: BarChart3, color: "from-cyan-500/20 to-cyan-500/5", description: "KPI та звіти" },
];

const RISK_BADGE: Record<string, "default" | "secondary" | "destructive"> = {
  low: "secondary",
  medium: "default",
  high: "destructive",
};

const RISK_LABEL: Record<string, string> = {
  low: "Низький ризик",
  medium: "Середній ризик",
  high: "Високий ризик",
};

// ─── Components ───────────────────────────────────────────────────────────

function AgentChat({ role }: { role: AgentRole }) {
  const [task, setTask] = useState("");
  const [response, setResponse] = useState("");
  const [loading, setLoading] = useState(false);

  const runAgent = async () => {
    if (!task.trim()) return;
    setLoading(true);
    setResponse("");
    try {
      const { data, error } = await supabase.functions.invoke("pablo-executive-brain", {
        body: { agent: role, task, include_business_context: true },
      });
      if (error) throw error;
      setResponse(data.response || "Немає відповіді");
      if (data.requires_approval) {
        toast.warning("Рішення потребує підтвердження", {
          description: "Переглянь вкладку «Підтвердження»",
        });
      }
    } catch (err) {
      toast.error("Помилка агента", { description: (err as Error).message });
    } finally {
      setLoading(false);
    }
  };

  const placeholders: Record<AgentRole, string> = {
    ceo: "Проаналізуй стан бізнесу і визнач 3 головних пріоритети на цей тиждень...",
    cmo: "Оціни ефективність останніх Telegram розсилок і запропонуй наступну кампанію...",
    cfo: "Розрахуй маржинальність по категоріях продуктів і вкажи де найгірша рентабельність...",
    coo: "Перевір статус доставок за останні 24 години, чи є затримки або відмови...",
    analyst: "Зроби тижневий аналіз воронки продажів: де найбільші втрати...",
    cos: "Що потребує уваги засновника сьогодні?",
  };

  return (
    <div className="space-y-3">
      <Textarea
        value={task}
        onChange={e => setTask(e.target.value)}
        placeholder={placeholders[role]}
        className="min-h-24 text-sm"
        onKeyDown={e => {
          if (e.key === "Enter" && e.ctrlKey) void runAgent();
        }}
      />
      <div className="flex items-center gap-2">
        <Button onClick={void runAgent} disabled={loading || !task.trim()} size="sm">
          {loading ? <Loader2 className="h-3 w-3 mr-1 animate-spin" /> : <Sparkles className="h-3 w-3 mr-1" />}
          Запустити
        </Button>
        <span className="text-xs text-muted-foreground">або Ctrl+Enter</span>
      </div>

      {response && (
        <div className="rounded-xl border border-border/60 bg-muted/30 p-4 text-sm prose prose-sm dark:prose-invert max-w-none">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{response}</ReactMarkdown>
        </div>
      )}
    </div>
  );
}

function ApprovalCard({ item, onDecision }: { item: ApprovalItem; onDecision: () => void }) {
  const [note, setNote] = useState("");
  const [processing, setProcessing] = useState(false);

  const decide = async (action: "approved" | "rejected") => {
    setProcessing(true);
    try {
      await supabase.from("pablo_approval_queue").update({
        status: action,
        reviewed_at: new Date().toISOString(),
        review_note: note || null,
      }).eq("id", item.id);

      // Update parent decision
      await supabase.from("pablo_executive_decisions").update({
        approval_status: action,
        approved_at: new Date().toISOString(),
      }).eq("id", item.decision_id);

      toast.success(action === "approved" ? "Схвалено ✓" : "Відхилено");
      onDecision();
    } catch (err) {
      toast.error("Помилка", { description: (err as Error).message });
    } finally {
      setProcessing(false);
    }
  };

  return (
    <Card className="border-l-4 border-l-primary/60">
      <CardContent className="pt-4 space-y-3">
        <div className="flex items-start justify-between gap-3">
          <div>
            <div className="flex items-center gap-2 mb-1">
              <Badge variant={RISK_BADGE[item.risk_level] || "default"}>
                {RISK_LABEL[item.risk_level] || item.risk_level}
              </Badge>
              <Badge variant="outline">{item.agent.toUpperCase()}</Badge>
              <Badge variant="outline">{item.action_type}</Badge>
            </div>
            <h3 className="font-semibold">{item.title}</h3>
          </div>
          <span className="text-xs text-muted-foreground whitespace-nowrap">
            {formatDistanceToNow(new Date(item.created_at), { locale: uk, addSuffix: true })}
          </span>
        </div>

        <p className="text-sm text-muted-foreground">{item.description}</p>

        <Textarea
          placeholder="Причина відхилення (необов'язково)..."
          value={note}
          onChange={e => setNote(e.target.value)}
          className="text-sm min-h-16"
        />

        <div className="flex gap-2">
          <Button
            size="sm"
            onClick={() => void decide("approved")}
            disabled={processing}
            className="gap-1"
          >
            <CheckCircle2 className="h-3 w-3" />
            Схвалити
          </Button>
          <Button
            size="sm"
            variant="destructive"
            onClick={() => void decide("rejected")}
            disabled={processing}
            className="gap-1"
          >
            <XCircle className="h-3 w-3" />
            Відхилити
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

// ─── Main Component ───────────────────────────────────────────────────────

export default function AdminPabloAI() {
  const qc = useQueryClient();
  const [selectedAgent, setSelectedAgent] = useState<AgentRole>("cos");
  const [briefingLoading, setBriefingLoading] = useState(false);

  // Fetch approval queue
  const { data: approvals = [], refetch: refetchApprovals } = useQuery<ApprovalItem[]>({
    queryKey: ["pablo_approvals"],
    queryFn: async () => {
      const { data } = await supabase
        .from("pablo_approval_queue")
        .select("*")
        .eq("status", "pending")
        .gt("expires_at", new Date().toISOString())
        .order("created_at", { ascending: false });
      return (data || []) as ApprovalItem[];
    },
    refetchInterval: 30_000,
  });

  // Fetch recent decisions
  const { data: decisions = [] } = useQuery<Decision[]>({
    queryKey: ["pablo_decisions"],
    queryFn: async () => {
      const { data } = await supabase
        .from("pablo_executive_decisions")
        .select("*")
        .order("created_at", { ascending: false })
        .limit(20);
      return (data || []) as Decision[];
    },
    refetchInterval: 30_000,
  });

  // Fetch latest briefing
  const { data: latestBriefing } = useQuery<Briefing | null>({
    queryKey: ["pablo_latest_briefing"],
    queryFn: async () => {
      const { data } = await supabase
        .from("pablo_briefings")
        .select("*")
        .order("created_at", { ascending: false })
        .limit(1)
        .maybeSingle();
      return (data as Briefing | null) ?? null;
    },
  });

  const runMorningBrief = async () => {
    setBriefingLoading(true);
    try {
      const { error } = await supabase.functions.invoke("pablo-morning-brief", {
        body: { source: "manual" },
      });
      if (error) throw error;
      toast.success("Ранковий брифінг готовий", { description: "Надіслано в Telegram" });
      void qc.invalidateQueries({ queryKey: ["pablo_latest_briefing"] });
    } catch (err) {
      toast.error("Помилка", { description: (err as Error).message });
    } finally {
      setBriefingLoading(false);
    }
  };

  const agentConfig = AGENTS.find(a => a.role === selectedAgent) || AGENTS[0];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-purple-500/30 to-blue-500/20 flex items-center justify-center">
            <Brain className="h-5 w-5 text-purple-400" />
          </div>
          <div>
            <h1 className="text-2xl font-bold">Pablo AI</h1>
            <p className="text-sm text-muted-foreground">Виконавча AI система для BASIC.FOOD</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {approvals.length > 0 && (
            <Badge variant="destructive" className="gap-1">
              <Clock className="h-3 w-3" />
              {approvals.length} рішень очікують
            </Badge>
          )}
          <Button size="sm" variant="outline" onClick={() => void runMorningBrief()} disabled={briefingLoading}>
            {briefingLoading ? <Loader2 className="h-3 w-3 mr-1 animate-spin" /> : <Sparkles className="h-3 w-3 mr-1" />}
            Ранковий брифінг
          </Button>
        </div>
      </div>

      {/* Approval Queue Alert */}
      {approvals.length > 0 && (
        <Card className="border-amber-500/50 bg-amber-500/5">
          <CardContent className="flex items-center gap-3 pt-4">
            <AlertTriangle className="h-5 w-5 text-amber-500 shrink-0" />
            <div>
              <p className="font-medium">{approvals.length} рішень Pablo AI очікують вашого підтвердження</p>
              <p className="text-sm text-muted-foreground">Перейди на вкладку «Підтвердження» щоб переглянути</p>
            </div>
          </CardContent>
        </Card>
      )}

      <Tabs defaultValue="agents">
        <TabsList className="w-full justify-start gap-1">
          <TabsTrigger value="agents">Агенти</TabsTrigger>
          <TabsTrigger value="approvals" className="relative">
            Підтвердження
            {approvals.length > 0 && (
              <span className="ml-1.5 h-4 w-4 rounded-full bg-destructive text-[10px] text-destructive-foreground flex items-center justify-center">
                {approvals.length}
              </span>
            )}
          </TabsTrigger>
          <TabsTrigger value="briefing">Брифінг</TabsTrigger>
          <TabsTrigger value="decisions">Журнал</TabsTrigger>
        </TabsList>

        {/* ── Agents Tab ── */}
        <TabsContent value="agents" className="space-y-4">
          <div className="grid grid-cols-2 md:grid-cols-5 gap-2">
            {AGENTS.map(agent => {
              const Icon = agent.icon;
              const active = agent.role === selectedAgent;
              return (
                <button
                  key={agent.role}
                  onClick={() => setSelectedAgent(agent.role)}
                  className={`rounded-xl p-3 text-left transition-all border ${
                    active
                      ? "bg-primary/15 border-primary/40"
                      : "bg-card/50 border-border/50 hover:border-primary/20"
                  }`}
                >
                  <div className={`w-8 h-8 rounded-lg bg-gradient-to-br ${agent.color} flex items-center justify-center mb-2`}>
                    <Icon className="h-4 w-4" />
                  </div>
                  <p className="font-semibold text-sm">{agent.label}</p>
                  <p className="text-[11px] text-muted-foreground">{agent.description}</p>
                </button>
              );
            })}
          </div>

          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-base">
                <agentConfig.icon className="h-4 w-4" />
                {agentConfig.label} Agent
              </CardTitle>
              <CardDescription>{agentConfig.description}</CardDescription>
            </CardHeader>
            <CardContent>
              <AgentChat role={selectedAgent} />
            </CardContent>
          </Card>
        </TabsContent>

        {/* ── Approvals Tab ── */}
        <TabsContent value="approvals" className="space-y-4">
          {approvals.length === 0 ? (
            <Card>
              <CardContent className="flex flex-col items-center gap-2 py-12 text-center text-muted-foreground">
                <CheckCircle2 className="h-8 w-8 text-emerald-500" />
                <p className="font-medium">Всі рішення оброблені</p>
                <p className="text-sm">Нових підтверджень не потрібно</p>
              </CardContent>
            </Card>
          ) : (
            approvals.map(item => (
              <ApprovalCard
                key={item.id}
                item={item}
                onDecision={() => {
                  void refetchApprovals();
                  void qc.invalidateQueries({ queryKey: ["pablo_decisions"] });
                }}
              />
            ))
          )}
        </TabsContent>

        {/* ── Briefing Tab ── */}
        <TabsContent value="briefing" className="space-y-4">
          {latestBriefing ? (
            <Card>
              <CardHeader>
                <div className="flex items-center justify-between">
                  <CardTitle className="text-base">{latestBriefing.title}</CardTitle>
                  <div className="flex items-center gap-2">
                    {latestBriefing.sent_to_tg && (
                      <Badge variant="secondary" className="gap-1 text-xs">
                        <MessageSquare className="h-3 w-3" /> Надіслано в TG
                      </Badge>
                    )}
                    <span className="text-xs text-muted-foreground">
                      {formatDistanceToNow(new Date(latestBriefing.created_at), { locale: uk, addSuffix: true })}
                    </span>
                  </div>
                </div>
              </CardHeader>
              <CardContent>
                <div className="prose prose-sm dark:prose-invert max-w-none">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{latestBriefing.content}</ReactMarkdown>
                </div>
              </CardContent>
            </Card>
          ) : (
            <Card>
              <CardContent className="flex flex-col items-center gap-3 py-12 text-center">
                <Sparkles className="h-8 w-8 text-muted-foreground" />
                <p className="font-medium">Брифінг ще не згенеровано</p>
                <Button onClick={() => void runMorningBrief()} disabled={briefingLoading} size="sm">
                  {briefingLoading ? <Loader2 className="h-3 w-3 mr-1 animate-spin" /> : null}
                  Генерувати зараз
                </Button>
              </CardContent>
            </Card>
          )}
        </TabsContent>

        {/* ── Decisions Log Tab ── */}
        <TabsContent value="decisions" className="space-y-3">
          {decisions.length === 0 ? (
            <Card>
              <CardContent className="py-12 text-center text-muted-foreground">
                <p>Журнал рішень порожній. Запусти агента щоб побачити рішення.</p>
              </CardContent>
            </Card>
          ) : (
            decisions.map(d => (
              <Card key={d.id} className="border-border/60">
                <CardContent className="pt-4 space-y-2">
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge variant="outline">{d.agent.toUpperCase()}</Badge>
                      <Badge variant={RISK_BADGE[d.risk_level] || "default"}>{RISK_LABEL[d.risk_level]}</Badge>
                      <Badge variant={
                        d.approval_status === "auto_executed" ? "secondary"
                          : d.approval_status === "approved" ? "default"
                          : d.approval_status === "rejected" ? "destructive"
                          : "outline"
                      }>
                        {d.approval_status === "auto_executed" ? "Виконано автоматично"
                          : d.approval_status === "approved" ? "Схвалено"
                          : d.approval_status === "rejected" ? "Відхилено"
                          : "Очікує"}
                      </Badge>
                    </div>
                    <span className="text-xs text-muted-foreground whitespace-nowrap">
                      {formatDistanceToNow(new Date(d.created_at), { locale: uk, addSuffix: true })}
                    </span>
                  </div>
                  <h3 className="font-medium">{d.title}</h3>
                  <p className="text-sm text-muted-foreground">{d.summary}</p>
                </CardContent>
              </Card>
            ))
          )}
        </TabsContent>
      </Tabs>
    </div>
  );
}
