import { Module } from '@nestjs/common';
import { JwtModule } from '@nestjs/jwt';
import { BullModule } from '@nestjs/bull';
import { AuthModule } from './auth/auth.module';
import { BotsModule } from './bots/bots.module';
import { ConversationsModule } from './conversations/conversations.module';
import { UsersModule } from './users/users.module';
import { BroadcastsModule } from './broadcasts/broadcasts.module';
import { AnalyticsModule } from './analytics/analytics.module';
import { InboxModule } from './inbox/inbox.module';
import { StatsModule } from './stats/stats.module';

@Module({
  imports: [
    JwtModule.register({
      global: true,
      secret: process.env.JWT_SECRET ?? 'fallback-secret',
      signOptions: { expiresIn: process.env.JWT_EXPIRES_IN ?? '15m' },
    }),
    BullModule.forRoot({ redis: process.env.REDIS_URL ?? 'redis://localhost:6379' }),
    BullModule.registerQueue({ name: 'broadcasts' }),
    AuthModule,
    BotsModule,
    ConversationsModule,
    UsersModule,
    BroadcastsModule,
    AnalyticsModule,
    InboxModule,
    StatsModule,
  ],
})
export class AppModule {}
