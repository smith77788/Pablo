import { Injectable, Logger } from '@nestjs/common';
import axios, { AxiosInstance } from 'axios';
import * as https from 'https';

@Injectable()
export class TelegramClient {
  private readonly logger = new Logger(TelegramClient.name);
  private readonly http: AxiosInstance;

  constructor() {
    this.http = axios.create({
      baseURL: 'https://api.telegram.org',
      timeout: 30_000,
      httpsAgent: new https.Agent({ rejectUnauthorized: false }),
    });
  }

  async call<T>(token: string, method: string, data?: Record<string, unknown>): Promise<T> {
    const url = `/bot${token}/${method}`;
    const resp = await this.http.post<{ ok: boolean; result: T; error_code?: number; description?: string }>(
      url, data ?? {},
    );
    if (!resp.data.ok) {
      throw new Error(`Telegram API error ${resp.data.error_code}: ${resp.data.description}`);
    }
    return resp.data.result;
  }

  async sendMessage(token: string, chatId: number, text: string, extra: Record<string, unknown> = {}) {
    return this.call(token, 'sendMessage', { chat_id: chatId, text, parse_mode: 'HTML', ...extra });
  }

  async sendPhoto(token: string, chatId: number, photo: string, caption?: string) {
    return this.call(token, 'sendPhoto', { chat_id: chatId, photo, caption, parse_mode: 'HTML' });
  }

  async setWebhook(token: string, url: string, secret: string) {
    return this.call(token, 'setWebhook', {
      url,
      secret_token: secret,
      allowed_updates: ['message', 'edited_message', 'callback_query', 'my_chat_member'],
      max_connections: 100,
    });
  }

  async deleteWebhook(token: string) {
    return this.call(token, 'deleteWebhook', { drop_pending_updates: false });
  }

  async getMe(token: string) {
    return this.call<{ id: number; username: string; first_name: string }>(token, 'getMe');
  }

  async forwardMessage(token: string, chatId: number, fromChatId: number, messageId: number) {
    return this.call(token, 'forwardMessage', {
      chat_id: chatId, from_chat_id: fromChatId, message_id: messageId,
    });
  }

  async copyMessage(token: string, chatId: number, fromChatId: number, messageId: number, extra: Record<string, unknown> = {}) {
    return this.call(token, 'copyMessage', {
      chat_id: chatId, from_chat_id: fromChatId, message_id: messageId, ...extra,
    });
  }
}
