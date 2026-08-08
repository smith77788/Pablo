import { Module } from '@nestjs/common';
import { BullModule } from '@nestjs/bull';
import { WebhookModule } from './webhook/webhook.module';
import { RelayModule } from './relay/relay.module';

@Module({
  imports: [
    BullModule.forRoot({
      redis: process.env.REDIS_URL ?? 'redis://localhost:6379',
    }),
    WebhookModule,
    RelayModule,
  ],
})
export class AppModule {}
