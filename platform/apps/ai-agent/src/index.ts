import Anthropic from '@anthropic-ai/sdk';
import { prisma } from '@platform/db';
import { clickhouse } from '@platform/clickhouse';

const client = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });

const SYSTEM_PROMPT = `You are an AI Growth Agent for a Telegram SaaS platform.
Your job is to analyze bot metrics and provide actionable growth recommendations.

You have access to these tools:
- get_bot_metrics: Get metrics for a specific bot (users, messages, retention)
- get_audience_stats: Get detailed audience breakdown by language, activity
- get_broadcast_performance: Get broadcast CTR and performance data
- suggest_optimization: Store an optimization suggestion for a bot

Always think step by step. Be specific and data-driven. Recommendations must be immediately actionable.
Focus on: activation rate, D1/D7/D30 retention, broadcast open rate, audience growth.`;

interface BotMetrics {
  botId: string;
  username: string;
  totalUsers: number;
  activeUsers7d: number;
  messagesIn7d: number;
  messagesOut7d: number;
  openConversations: number;
  retentionD1: number;
  retentionD7: number;
}

async function getBotMetrics(botId: string): Promise<BotMetrics> {
  const [bot, totalUsers, openConvs] = await Promise.all([
    prisma.bot.findUnique({ where: { id: botId } }),
    prisma.telegramUser.count({ where: { firstBotId: botId, isBlocked: false } }),
    prisma.conversation.count({ where: { botId, status: 'OPEN' } }),
  ]);

  let activeUsers7d = 0, messagesIn7d = 0, messagesOut7d = 0;
  try {
    const result = await clickhouse.query({
      query: `
        SELECT
          uniqIf(user_id, event_type = 'message_received') as active,
          countIf(event_type = 'message_received') as msg_in,
          countIf(event_type = 'message_sent') as msg_out
        FROM events
        WHERE bot_id = {botId:String} AND timestamp >= now() - INTERVAL 7 DAY
      `,
      query_params: { botId },
      format: 'JSONEachRow',
    });
    const rows = await result.json<any[]>();
    if (rows.length) {
      activeUsers7d = rows[0].active;
      messagesIn7d = rows[0].msg_in;
      messagesOut7d = rows[0].msg_out;
    }
  } catch { /* ClickHouse may be unavailable */ }

  return {
    botId,
    username: bot?.username ?? bot?.firstName ?? 'unknown',
    totalUsers,
    activeUsers7d,
    messagesIn7d,
    messagesOut7d,
    openConversations: openConvs,
    retentionD1: 0,
    retentionD7: 0,
  };
}

const tools: Anthropic.Tool[] = [
  {
    name: 'get_bot_metrics',
    description: 'Get performance metrics for a Telegram bot',
    input_schema: {
      type: 'object' as const,
      properties: {
        bot_id: { type: 'string', description: 'The bot UUID' },
      },
      required: ['bot_id'],
    },
  },
  {
    name: 'get_all_bots',
    description: 'Get list of all active bots in the platform for analysis',
    input_schema: {
      type: 'object' as const,
      properties: {
        tenant_id: { type: 'string', description: 'Tenant UUID to filter by' },
      },
      required: [],
    },
  },
  {
    name: 'store_recommendation',
    description: 'Store a growth recommendation for a specific bot',
    input_schema: {
      type: 'object' as const,
      properties: {
        bot_id: { type: 'string' },
        category: { type: 'string', enum: ['retention', 'activation', 'broadcast', 'growth', 'onboarding'] },
        priority: { type: 'string', enum: ['high', 'medium', 'low'] },
        title: { type: 'string' },
        description: { type: 'string' },
        action: { type: 'string', description: 'Specific action to take' },
      },
      required: ['bot_id', 'category', 'priority', 'title', 'description', 'action'],
    },
  },
];

async function runTool(name: string, input: Record<string, string>): Promise<string> {
  if (name === 'get_bot_metrics') {
    const metrics = await getBotMetrics(input.bot_id);
    return JSON.stringify(metrics, null, 2);
  }

  if (name === 'get_all_bots') {
    const where: any = { isActive: true };
    if (input.tenant_id) where.tenantId = input.tenant_id;
    const bots = await prisma.bot.findMany({
      where,
      select: { id: true, username: true, firstName: true, tenantId: true, createdAt: true },
      take: 20,
    });
    return JSON.stringify(bots, null, 2);
  }

  if (name === 'store_recommendation') {
    // Store as automation note or in a custom field on the bot
    console.log(`[AI Recommendation] Bot ${input.bot_id}: [${input.priority}] ${input.title}`);
    console.log(`  Category: ${input.category}`);
    console.log(`  ${input.description}`);
    console.log(`  Action: ${input.action}`);
    return JSON.stringify({ stored: true });
  }

  return JSON.stringify({ error: 'Unknown tool' });
}

export async function runGrowthAnalysis(tenantId?: string): Promise<void> {
  console.log('AI Growth Agent starting analysis...');

  const messages: Anthropic.MessageParam[] = [
    {
      role: 'user',
      content: `Analyze the Telegram bots on this platform${tenantId ? ` for tenant ${tenantId}` : ''}.

      1. Get the list of all bots
      2. For each bot, get metrics
      3. Identify the top 3 growth opportunities per bot
      4. Store specific, actionable recommendations

      Focus on: low activation rate, poor retention, inactive users, underperforming broadcasts.
      Be concise and data-driven.`,
    },
  ];

  let response = await client.messages.create({
    model: 'claude-sonnet-4-6',
    max_tokens: 4096,
    system: SYSTEM_PROMPT,
    tools,
    messages,
  });

  while (response.stop_reason === 'tool_use') {
    const toolUses = response.content.filter((b): b is Anthropic.ToolUseBlock => b.type === 'tool_use');

    messages.push({ role: 'assistant', content: response.content });

    const toolResults: Anthropic.ToolResultBlockParam[] = await Promise.all(
      toolUses.map(async (tool) => ({
        type: 'tool_result' as const,
        tool_use_id: tool.id,
        content: await runTool(tool.name, tool.input as Record<string, string>),
      }))
    );

    messages.push({ role: 'user', content: toolResults });

    response = await client.messages.create({
      model: 'claude-sonnet-4-6',
      max_tokens: 4096,
      system: SYSTEM_PROMPT,
      tools,
      messages,
    });
  }

  const finalText = response.content
    .filter((b): b is Anthropic.TextBlock => b.type === 'text')
    .map((b) => b.text)
    .join('\n');

  console.log('\nAnalysis complete:\n', finalText);
}

// Run if called directly
if (process.argv[1] === new URL(import.meta.url).pathname) {
  runGrowthAnalysis()
    .then(() => process.exit(0))
    .catch((err) => { console.error(err); process.exit(1); });
}
