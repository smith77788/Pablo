import {
  WebSocketGateway, WebSocketServer, SubscribeMessage,
  OnGatewayConnection, OnGatewayDisconnect, ConnectedSocket, MessageBody,
  OnGatewayInit,
} from '@nestjs/websockets';
import { Server, Socket } from 'socket.io';
import { JwtService } from '@nestjs/jwt';
import { Logger, OnModuleDestroy } from '@nestjs/common';
import Redis from 'ioredis';

const INBOX_CHANNEL = 'botmother:inbox';

@WebSocketGateway({ cors: { origin: '*' }, namespace: '/inbox' })
export class InboxGateway
  implements OnGatewayInit, OnGatewayConnection, OnGatewayDisconnect, OnModuleDestroy
{
  @WebSocketServer() server: Server;
  private readonly logger = new Logger(InboxGateway.name);
  private readonly redisSub: Redis;

  constructor(private readonly jwt: JwtService) {
    this.redisSub = new Redis(process.env.REDIS_URL ?? 'redis://localhost:6379', { lazyConnect: true });
  }

  async afterInit() {
    await this.redisSub.connect().catch(() => {});
    await this.redisSub.subscribe(INBOX_CHANNEL);
    this.redisSub.on('message', (_ch, raw) => {
      try {
        const data = JSON.parse(raw);
        if (data.tenantId) this.emitToTenant(data.tenantId, 'inbox:new_message', data);
        if (data.conversationId) this.emitToConversation(data.conversationId, 'conv:new_message', data.message);
      } catch {
        this.logger.warn('Failed to parse inbox redis event');
      }
    });
    this.logger.log('InboxGateway subscribed to Redis inbox channel');
  }

  async onModuleDestroy() {
    await this.redisSub.unsubscribe(INBOX_CHANNEL);
    await this.redisSub.quit();
  }

  handleConnection(client: Socket) {
    const token = client.handshake.auth?.token ?? client.handshake.headers?.authorization?.replace('Bearer ', '');
    if (!token) { client.disconnect(); return; }
    try {
      const payload = this.jwt.verify(token);
      client.data.user = payload;
      client.join(`tenant:${payload.tenantId}`);
    } catch {
      client.disconnect();
    }
  }

  handleDisconnect(client: Socket) {
    client.leave(`tenant:${client.data.user?.tenantId}`);
  }

  @SubscribeMessage('join:conversation')
  joinConversation(@ConnectedSocket() client: Socket, @MessageBody() data: { conversationId: string }) {
    client.join(`conv:${data.conversationId}`);
  }

  @SubscribeMessage('leave:conversation')
  leaveConversation(@ConnectedSocket() client: Socket, @MessageBody() data: { conversationId: string }) {
    client.leave(`conv:${data.conversationId}`);
  }

  emitToTenant(tenantId: string, event: string, data: unknown) {
    this.server.to(`tenant:${tenantId}`).emit(event, data);
  }

  emitToConversation(conversationId: string, event: string, data: unknown) {
    this.server.to(`conv:${conversationId}`).emit(event, data);
  }
}
