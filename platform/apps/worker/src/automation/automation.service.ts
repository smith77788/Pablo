import { Injectable, Logger } from '@nestjs/common';
import { prisma } from '@platform/db';
import { InjectQueue } from '@nestjs/bull';
import { Queue } from 'bull';

@Injectable()
export class AutomationService {
  private readonly logger = new Logger(AutomationService.name);

  constructor(@InjectQueue('automation') private readonly queue: Queue) {}

  async trigger(event: {
    type: string; tenantId: string; botId: string;
    userId?: string; conversationId?: string; payload?: Record<string, unknown>;
  }): Promise<void> {
    const rules = await prisma.automation.findMany({
      where: { tenantId: event.tenantId, isActive: true },
    });

    for (const rule of rules) {
      const trigger = rule.trigger as any;
      if (trigger.type !== event.type) continue;

      // Check conditions
      const conditions = (rule.conditions as any[]) ?? [];
      const matches = conditions.every((cond) => this.evaluateCondition(cond, event));
      if (!matches) continue;

      await this.queue.add('execute', {
        automationId: rule.id,
        actions: rule.actions,
        event,
      }, { attempts: 2, removeOnComplete: 100 });
    }
  }

  private evaluateCondition(cond: any, event: any): boolean {
    const { field, operator, value } = cond;
    const actual = event.payload?.[field];
    if (operator === 'eq') return actual === value;
    if (operator === 'contains') return String(actual ?? '').includes(value);
    if (operator === 'exists') return actual !== undefined;
    return true;
  }
}
