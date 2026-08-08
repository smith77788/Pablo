import {
  Controller, Post, Param, Body, Headers, HttpCode, ForbiddenException,
} from '@nestjs/common';
import { WebhookService } from './webhook.service';
import { TelegramUpdate } from '@platform/types';
import * as crypto from 'crypto';

@Controller('webhook')
export class WebhookController {
  constructor(private readonly webhookService: WebhookService) {}

  @Post(':botToken')
  @HttpCode(200)
  async handleUpdate(
    @Param('botToken') botToken: string,
    @Body() update: TelegramUpdate,
    @Headers('x-telegram-bot-api-secret-token') secret: string,
  ): Promise<{ ok: boolean }> {
    const expected = process.env.TELEGRAM_WEBHOOK_SECRET ?? '';
    if (expected && secret !== expected) {
      throw new ForbiddenException('Invalid secret token');
    }

    await this.webhookService.enqueue(botToken, update);
    return { ok: true };
  }
}
