import { Injectable } from '@nestjs/common';

export interface StatsOverview {
  totalUsers: number;
  newToday: number;
  messagesSent: number;
  messagesReceived: number;
  activeFunnels: number;
  activeReplies: number;
}

@Injectable()
export class StatsService {
  getOverview(): StatsOverview {
    // Mock data — replace with real DB queries when schema is ready
    return {
      totalUsers: 0,
      newToday: 0,
      messagesSent: 0,
      messagesReceived: 0,
      activeFunnels: 0,
      activeReplies: 0,
    };
  }
}
