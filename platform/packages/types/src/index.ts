// Shared TypeScript types across all services

export interface TelegramUpdate {
  update_id: number;
  message?: TelegramMessage;
  edited_message?: TelegramMessage;
  callback_query?: TelegramCallbackQuery;
  my_chat_member?: TelegramChatMemberUpdated;
}

export interface TelegramMessage {
  message_id: number;
  from?: TelegramUser;
  chat: TelegramChat;
  date: number;
  text?: string;
  caption?: string;
  photo?: TelegramPhotoSize[];
  video?: TelegramVideo;
  audio?: TelegramAudio;
  voice?: TelegramVoice;
  document?: TelegramDocument;
  sticker?: TelegramSticker;
  animation?: TelegramAnimation;
  location?: TelegramLocation;
  contact?: TelegramContact;
  reply_to_message?: TelegramMessage;
}

export interface TelegramUser {
  id: number;
  is_bot: boolean;
  first_name: string;
  last_name?: string;
  username?: string;
  language_code?: string;
  is_premium?: boolean;
}

export interface TelegramChat {
  id: number;
  type: 'private' | 'group' | 'supergroup' | 'channel';
  first_name?: string;
  username?: string;
}

export interface TelegramCallbackQuery {
  id: string;
  from: TelegramUser;
  message?: TelegramMessage;
  data?: string;
}

export interface TelegramPhotoSize {
  file_id: string;
  file_unique_id: string;
  width: number;
  height: number;
  file_size?: number;
}

export interface TelegramVideo {
  file_id: string;
  file_unique_id: string;
  width: number;
  height: number;
  duration: number;
  file_size?: number;
  mime_type?: string;
}

export interface TelegramAudio {
  file_id: string;
  file_unique_id: string;
  duration: number;
  performer?: string;
  title?: string;
  file_size?: number;
  mime_type?: string;
}

export interface TelegramVoice {
  file_id: string;
  file_unique_id: string;
  duration: number;
  file_size?: number;
  mime_type?: string;
}

export interface TelegramDocument {
  file_id: string;
  file_unique_id: string;
  file_name?: string;
  mime_type?: string;
  file_size?: number;
}

export interface TelegramSticker {
  file_id: string;
  file_unique_id: string;
  width: number;
  height: number;
  is_animated: boolean;
  is_video: boolean;
}

export interface TelegramAnimation {
  file_id: string;
  file_unique_id: string;
  width: number;
  height: number;
  duration: number;
}

export interface TelegramLocation {
  longitude: number;
  latitude: number;
  horizontal_accuracy?: number;
}

export interface TelegramContact {
  phone_number: string;
  first_name: string;
  last_name?: string;
  user_id?: number;
}

export interface TelegramChatMemberUpdated {
  chat: TelegramChat;
  from: TelegramUser;
  date: number;
  old_chat_member: { status: string };
  new_chat_member: { status: string };
}

// ─── Platform types ─────────────────────────────────────────────────────────

export interface PlatformEvent {
  id: string;
  type: PlatformEventType;
  tenantId: string;
  botId: string;
  payload: Record<string, unknown>;
  timestamp: Date;
}

export type PlatformEventType =
  | 'message.received'
  | 'message.sent'
  | 'message.edited'
  | 'message.deleted'
  | 'conversation.opened'
  | 'conversation.assigned'
  | 'conversation.resolved'
  | 'user.created'
  | 'user.blocked'
  | 'broadcast.started'
  | 'broadcast.completed'
  | 'automation.triggered';

export interface JwtPayload {
  sub: string;        // operatorId
  tenantId: string;
  email: string;
  role: string;
}
