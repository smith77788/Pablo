import { Processor, Process } from '@nestjs/bull';
import { Job } from 'bull';
import { Logger } from '@nestjs/common';
import { prisma } from '@platform/db';
import axios from 'axios';
import * as https from 'https';

const tgHttp = axios.create({ httpsAgent: new https.Agent({ rejectUnauthorized: false }), timeout: 10_000 });

@Processor('automation')
export class AutomationProcessor {
  private readonly logger = new Logger(AutomationProcessor.name);

  @Process('execute')
  async execute(job: Job<{ automationId: string; actions: any; event: any }>): Promise<void> {
    const { automationId, actions, event } = job.data;
    const actionList = Array.isArray(actions) ? actions : [actions];

    for (const action of actionList) {
      await this.runAction(action, event);
    }

    await prisma.automation.update({
      where: { id: automationId },
      data: { runCount: { increment: 1 } },
    });
  }

  private async runAction(action: any, event: any): Promise<void> {
    switch (action.type) {
      case 'send_message': {
        const conv = event.conversationId
          ? await prisma.conversation.findUnique({ where: { id: event.conversationId }, include: { bot: true, user: true } })
          : null;
        if (conv) {
          await tgHttp.post(`https://api.telegram.org/bot${conv.bot.token}/sendMessage`, {
            chat_id: conv.user.telegramId.toString(),
            text: action.text,
            parse_mode: 'HTML',
          });
        }
        break;
      }
      case 'assign_operator': {
        if (event.conversationId) {
          await prisma.conversation.update({
            where: { id: event.conversationId },
            data: { assignedToId: action.operatorId },
          });
        }
        break;
      }
      case 'add_tag': {
        if (event.userId && action.tagId) {
          await prisma.userTag.create({
            data: { userId: event.userId, tagId: action.tagId },
          }).catch(() => {});
        }
        break;
      }
      case 'call_webhook': {
        await axios.post(action.url, { event }, {
          headers: { 'Content-Type': 'application/json' },
          timeout: 10_000,
        }).catch(() => {});
        break;
      }
      case 'update_status': {
        if (event.conversationId) {
          await prisma.conversation.update({
            where: { id: event.conversationId },
            data: { status: action.status },
          });
        }
        break;
      }
    }
  }
}
