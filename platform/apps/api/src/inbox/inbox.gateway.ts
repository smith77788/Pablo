import {
  WebSocketGateway, WebSocketServer, SubscribeMessage,
  OnGatewayConnection, OnGatewayDisconnect, ConnectedSocket, MessageBody,
} from '@nestjs/websockets';
import { Server, Socket } from 'socket.io';
import { JwtService } from '@nestjs/jwt';

@WebSocketGateway({ cors: { origin: '*' }, namespace: '/inbox' })
export class InboxGateway implements OnGatewayConnection, OnGatewayDisconnect {
  @WebSocketServer() server: Server;

  constructor(private readonly jwt: JwtService) {}

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

  // Called by workers/services to push events to connected operators
  emitToTenant(tenantId: string, event: string, data: unknown) {
    this.server.to(`tenant:${tenantId}`).emit(event, data);
  }

  emitToConversation(conversationId: string, event: string, data: unknown) {
    this.server.to(`conv:${conversationId}`).emit(event, data);
  }
}
