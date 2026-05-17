// ────────────────────────────────────────────────────────────────────────────
// Nevesty Models — TypeScript Types
// Auto-generated from supabase/migrations/001_initial_schema.sql
// ────────────────────────────────────────────────────────────────────────────

// ── Enums / Union Types ─────────────────────────────────────────────────────

export type OrderStatus = 'new' | 'confirmed' | 'completed' | 'cancelled' | 'in_review';

export type EventType =
  | 'photo'
  | 'video'
  | 'event'
  | 'promo'
  | 'fashion'
  | 'commercial'
  | 'other';

export type ModelCategory = 'fashion' | 'commercial' | 'events';

export type ReviewStatus = 'pending' | 'approved' | 'rejected';

export type LoyaltyLevel = 'bronze' | 'silver' | 'gold' | 'platinum';

export type NotificationStatus = 'pending' | 'sent' | 'failed';

export type AbExperimentStatus = 'proposed' | 'running' | 'paused' | 'completed' | 'archived';

export type AbExperimentType = 'bot' | 'web' | 'both';

export type SocialPostStatus = 'draft' | 'scheduled' | 'published' | 'failed';

export type BroadcastSegment = 'all' | 'active' | 'inactive' | 'vip';

export type AdminRole = 'admin' | 'manager' | 'viewer';

export type DiscountType = 'percent' | 'fixed';

export type ModelAvailabilityStatus = 'free' | 'busy' | 'vacation';

export type AgentFindingSeverity = 'critical' | 'high' | 'medium' | 'low' | 'info';

export type AgentFindingStatus = 'open' | 'claimed' | 'fixed' | 'wontfix';

export type AuditAction = string;

export type SupportDirection = 'client_to_admin' | 'admin_to_client';

// ── Database Row Types ──────────────────────────────────────────────────────

/**
 * admins — system administrators / managers
 * Note: password_hash is excluded from the frontend type for security.
 */
export interface Admin {
  id: number;
  username: string;
  email: string | null;
  telegram_id: string | null;
  role: AdminRole;
  totp_secret: string | null;
  totp_enabled: boolean;
  last_login: string | null;
  active: boolean;
  created_at: string;
}

/**
 * models — the talent catalog
 * `photos` is stored as a JSON string in the DB; parse before use.
 */
export interface Model {
  id: number;
  name: string;
  age: number | null;
  height: number | null;
  weight: number | null;
  bust: number | null;
  waist: number | null;
  hips: number | null;
  shoe_size: string | null;
  hair_color: string | null;
  eye_color: string | null;
  bio: string | null;
  photo_main: string | null;
  /** JSON-encoded string[]; parse with JSON.parse(photos) */
  photos: string;
  instagram: string | null;
  category: ModelCategory | string;
  available: boolean;
  city: string | null;
  featured: boolean;
  phone: string | null;
  order_count: number;
  view_count: number;
  archived: boolean;
  video_url: string | null;
  telegram_chat_id: string | null;
  created_at: string;
}

/** orders — client booking requests */
export interface Order {
  id: number;
  order_number: string;
  client_name: string;
  client_phone: string;
  client_email: string | null;
  client_telegram: string | null;
  client_chat_id: string | null;
  model_id: number | null;
  event_type: EventType | string;
  event_date: string | null;
  event_duration: number;
  location: string | null;
  budget: string | null;
  comments: string | null;
  status: OrderStatus;
  admin_notes: string | null;
  manager_id: number | null;
  review_requested: string | null;
  /** JSON-encoded array of additional model IDs */
  model_ids: string | null;
  reminder_sent_at: string | null;
  utm_source: string;
  utm_medium: string;
  utm_campaign: string;
  payment_id: string | null;
  payment_status: string | null;
  paid_at: string | null;
  payment_url: string | null;
  payment_amount: number | null;
  invoice_sent_at: string | null;
  internal_note: string | null;
  completed_at: string | null;
  cancelled_at: string | null;
  reminded_at: string | null;
  review_invitation_sent_at: string | null;
  reminder_24h_sent: string | null;
  event_time: string | null;
  completed_reminder_sent: string | null;
  deposit_amount: number | null;
  stars_payment_charge_id: string | null;
  promo_code_id: number | null;
  discount_amount: number | null;
  created_at: string;
  updated_at: string;
}

/** reviews — client feedback */
export interface Review {
  id: number;
  client_name: string;
  rating: number;
  text: string;
  model_id: number | null;
  approved: boolean;
  status: ReviewStatus;
  order_id: number | null;
  chat_id: string | null;
  admin_reply: string | null;
  reply_at: string | null;
  rejected: boolean;
  created_at: string;
}

/** bot_settings — key/value configuration for the Telegram bot */
export interface BotSetting {
  key: string;
  value: string | null;
  updated_at: string;
}

/** ab_experiments — A/B test definitions */
export interface AbExperiment {
  id: string;
  hypothesis: string;
  type: AbExperimentType;
  metric: string | null;
  variant_a: string | null;
  variant_b: string | null;
  effort: string | null;
  expected_lift: string | null;
  status: AbExperimentStatus;
  recommendation: string | null;
  eval_reason: string | null;
  department: string;
  created_at: string;
  updated_at: string;
}

/** notifications — queued messages to be dispatched */
export interface Notification {
  id: number;
  chat_id: string | null;
  type: string;
  /** JSON-encoded payload object */
  payload: string;
  status: NotificationStatus;
  created_at: string;
  sent_at: string | null;
}

/** loyalty_transactions — point history per client */
export interface LoyaltyTransaction {
  id: number;
  chat_id: number;
  points: number;
  type: string;
  description: string | null;
  order_id: number | null;
  created_at: string;
}

/** model_photos — gallery images for a model */
export interface ModelPhoto {
  id: number;
  model_id: number;
  filename: string;
  url: string;
  is_cover: boolean;
  sort_order: number;
  uploaded_at: string;
}

/** price_packages — service tiers shown to clients */
export interface PricePackage {
  id: number;
  name: string;
  description: string | null;
  price_from: number;
  price_to: number | null;
  duration: string | null;
  category: string;
  sort_order: number;
  active: boolean;
}

/** referrals — referral program tracking */
export interface Referral {
  id: number;
  referrer_chat_id: number;
  referred_chat_id: number;
  bonus_points: number;
  created_at: string;
}

/** order_notes — internal notes attached to an order */
export interface OrderNote {
  id: number;
  order_id: number;
  admin_note: string;
  created_at: string;
}

/** agent_logs — messages logged by AI agents */
export interface AgentLog {
  id: number;
  from_name: string | null;
  message: string;
  created_at: string;
}

/** wishlists — models saved by clients (Telegram) */
export interface Wishlist {
  id: number;
  chat_id: string;
  model_id: number;
  created_at: string;
}

/** faq — frequently asked questions */
export interface Faq {
  id: number;
  question: string;
  answer: string;
  sort_order: number;
  active: boolean;
  category: string;
  created_at: string;
}

/** promo_codes — discount codes for orders */
export interface PromoCode {
  id: number;
  code: string;
  discount_type: DiscountType | string;
  discount_value: number;
  max_uses: number | null;
  used_count: number;
  valid_from: string | null;
  valid_until: string | null;
  is_active: boolean;
  created_by: number | null;
  created_at: string;
}

// ── Additional Table Types ──────────────────────────────────────────────────

/** messages — chat messages tied to an order */
export interface Message {
  id: number;
  order_id: number;
  sender_type: string;
  sender_name: string | null;
  content: string;
  created_at: string;
}

/** telegram_sessions — bot conversation state */
export interface TelegramSession {
  chat_id: string;
  state: string;
  order_id: number | null;
  /** JSON-encoded session data object */
  data: string;
  updated_at: string;
}

/** agent_findings — issues discovered by AI agents */
export interface AgentFinding {
  id: number;
  agent_name: string;
  severity: AgentFindingSeverity | string;
  message: string;
  file: string | null;
  line: number | null;
  auto_fixable: boolean;
  proposed_fix: string | null;
  status: AgentFindingStatus | string;
  claimed_by: string | null;
  claimed_at: string | null;
  fixed_by: string | null;
  fix_summary: string | null;
  fixed_at: string | null;
  created_at: string;
}

/** agent_discussions — inter-agent communication threads */
export interface AgentDiscussion {
  id: number;
  from_agent: string;
  to_agent: string;
  topic: string;
  message: string;
  ref_finding_id: number | null;
  created_at: string;
}

/** order_status_history — audit trail for status changes */
export interface OrderStatusHistory {
  id: number;
  order_id: number;
  old_status: string | null;
  new_status: string;
  changed_by: string | null;
  notes: string | null;
  created_at: string;
}

/** loyalty_points — running point balance per client */
export interface LoyaltyPoints {
  id: number;
  chat_id: number;
  points: number;
  total_earned: number;
  level: LoyaltyLevel | string;
  updated_at: string;
}

/** favorites — models bookmarked by Telegram clients */
export interface Favorite {
  id: number;
  chat_id: string;
  model_id: number;
  created_at: string;
}

/** client_prefs — per-client notification and UI preferences */
export interface ClientPrefs {
  chat_id: number;
  notify_status: boolean;
  notify_promo: boolean;
  notify_review: boolean;
  notify_reminders: boolean;
  notify_marketing: boolean;
  notify_review_invites: boolean;
  profile_hidden: boolean;
  language: string;
  name: string | null;
  phone: string | null;
  email: string | null;
  avatar_url: string | null;
  updated_at: string;
}

/** audit_log — admin action history */
export interface AuditLog {
  id: number;
  admin_chat_id: number;
  action: string;
  entity_type: string | null;
  entity_id: number | null;
  details: string | null;
  admin_username: string | null;
  entity: string | null;
  ip: string | null;
  created_at: string;
}

/** model_busy_dates — dates when a model is unavailable */
export interface ModelBusyDate {
  id: number;
  model_id: number;
  busy_date: string;
  reason: string | null;
  order_id: number | null;
  created_at: string;
}

/** social_posts — scheduled / published social media content */
export interface SocialPost {
  id: number;
  platform: string;
  model_id: number | null;
  content_type: string;
  caption: string | null;
  media_url: string | null;
  hashtags: string | null;
  scheduled_at: string | null;
  published_at: string | null;
  platform_post_id: string | null;
  /** JSON-encoded metrics object */
  metrics: string;
  status: SocialPostStatus | string;
  factory_cycle_id: string | null;
  created_at: string;
}

/** model_availability — current availability status of a model */
export interface ModelAvailability {
  id: number;
  model_id: number;
  status: ModelAvailabilityStatus | string;
  date_from: string | null;
  date_to: string | null;
  reason: string | null;
  created_at: string;
  updated_at: string;
}

/** scheduled_broadcasts — future mass messages */
export interface ScheduledBroadcast {
  id: number;
  text: string;
  scheduled_at: string;
  segment: BroadcastSegment | string;
  status: string;
  created_by: string | null;
  sent_count: number;
  error_count: number;
  sent_at: string | null;
  photo_url: string | null;
  created_at: string;
}

/** bot_broadcasts — completed broadcast campaigns */
export interface BotBroadcast {
  id: number;
  message: string;
  photo_id: string | null;
  segment: BroadcastSegment | string;
  sent_by: string | null;
  total_recipients: number;
  delivered: number;
  failed: number;
  skipped: number;
  status: string;
  started_at: string;
  finished_at: string | null;
}

/** model_availability_schedule — per-date availability overrides */
export interface ModelAvailabilitySchedule {
  id: number;
  model_id: number;
  date: string;
  is_available: boolean;
  reason: string | null;
  order_id: number | null;
  created_at: string;
}

/** support_messages — in-app support chat */
export interface SupportMessage {
  id: number;
  from_chat_id: number;
  to_chat_id: number | null;
  message: string;
  direction: SupportDirection;
  is_read: boolean;
  created_at: string;
}

/** quick_bookings — lightweight booking without full order flow */
export interface QuickBooking {
  id: number;
  client_name: string;
  client_phone: string;
  chat_id: string | null;
  status: string;
  created_at: string;
}

// ── Insert / Update Types ───────────────────────────────────────────────────

export type ModelInsert = Omit<Model, 'id' | 'created_at' | 'order_count' | 'view_count'>;
export type ModelUpdate = Partial<ModelInsert>;

export type OrderInsert = Omit<Order, 'id' | 'created_at' | 'order_number'> & {
  order_number?: string;
};
export type OrderUpdate = Partial<OrderInsert>;

export type ReviewInsert = Omit<Review, 'id' | 'created_at'>;
export type ReviewUpdate = Partial<ReviewInsert>;

export type AdminInsert = Omit<Admin, 'id' | 'created_at'> & { password_hash: string };
export type AdminUpdate = Partial<Omit<AdminInsert, 'password_hash'>>;

export type AbExperimentInsert = Omit<AbExperiment, 'created_at' | 'updated_at'>;
export type AbExperimentUpdate = Partial<AbExperimentInsert>;

export type NotificationInsert = Omit<Notification, 'id' | 'created_at'>;
export type NotificationUpdate = Partial<NotificationInsert>;

export type LoyaltyTransactionInsert = Omit<LoyaltyTransaction, 'id' | 'created_at'>;

export type ModelPhotoInsert = Omit<ModelPhoto, 'id' | 'uploaded_at'>;
export type ModelPhotoUpdate = Partial<ModelPhotoInsert>;

export type PricePackageInsert = Omit<PricePackage, 'id'>;
export type PricePackageUpdate = Partial<PricePackageInsert>;

export type ReferralInsert = Omit<Referral, 'id' | 'created_at'>;

export type OrderNoteInsert = Omit<OrderNote, 'id' | 'created_at'>;

export type AgentLogInsert = Omit<AgentLog, 'id' | 'created_at'>;

export type WishlistInsert = Omit<Wishlist, 'id' | 'created_at'>;

export type FaqInsert = Omit<Faq, 'id' | 'created_at'>;
export type FaqUpdate = Partial<FaqInsert>;

export type PromoCodeInsert = Omit<PromoCode, 'id' | 'created_at' | 'used_count'>;
export type PromoCodeUpdate = Partial<PromoCodeInsert>;

// ── API Response Types ──────────────────────────────────────────────────────

export interface ApiResponse<T = unknown> {
  success: boolean;
  data?: T;
  error?: string;
  message?: string;
}

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  per_page: number;
  total_pages: number;
}

// ── Filter / Query Types ────────────────────────────────────────────────────

export interface ModelFilters {
  category?: ModelCategory | string;
  city?: string;
  available?: boolean;
  featured?: boolean;
  min_height?: number;
  max_height?: number;
  min_age?: number;
  max_age?: number;
  search?: string;
  archived?: boolean;
}

export interface OrderFilters {
  status?: OrderStatus;
  model_id?: number;
  from_date?: string;
  to_date?: string;
  search?: string;
  manager_id?: number;
  payment_status?: string;
}

export interface ReviewFilters {
  status?: ReviewStatus;
  model_id?: number;
  approved?: boolean;
  search?: string;
}

export interface PaginationParams {
  page?: number;
  per_page?: number;
  sort_by?: string;
  sort_dir?: 'asc' | 'desc';
}

// ── Supabase Database Type ──────────────────────────────────────────────────
// For use with createClient<Database>()

export interface Database {
  public: {
    Tables: {
      admins: {
        Row: Admin & { password_hash: string };
        Insert: AdminInsert;
        Update: AdminUpdate & { password_hash?: string };
      };
      models: {
        Row: Model;
        Insert: ModelInsert;
        Update: ModelUpdate;
      };
      orders: {
        Row: Order;
        Insert: OrderInsert;
        Update: OrderUpdate;
      };
      reviews: {
        Row: Review;
        Insert: ReviewInsert;
        Update: ReviewUpdate;
      };
      bot_settings: {
        Row: BotSetting;
        Insert: Omit<BotSetting, 'updated_at'>;
        Update: { value: string };
      };
      ab_experiments: {
        Row: AbExperiment;
        Insert: AbExperimentInsert;
        Update: AbExperimentUpdate;
      };
      notifications: {
        Row: Notification;
        Insert: NotificationInsert;
        Update: NotificationUpdate;
      };
      loyalty_transactions: {
        Row: LoyaltyTransaction;
        Insert: LoyaltyTransactionInsert;
        Update: Partial<LoyaltyTransactionInsert>;
      };
      model_photos: {
        Row: ModelPhoto;
        Insert: ModelPhotoInsert;
        Update: ModelPhotoUpdate;
      };
      price_packages: {
        Row: PricePackage;
        Insert: PricePackageInsert;
        Update: PricePackageUpdate;
      };
      referrals: {
        Row: Referral;
        Insert: ReferralInsert;
        Update: Partial<ReferralInsert>;
      };
      order_notes: {
        Row: OrderNote;
        Insert: OrderNoteInsert;
        Update: Partial<OrderNoteInsert>;
      };
      agent_logs: {
        Row: AgentLog;
        Insert: AgentLogInsert;
        Update: Partial<AgentLogInsert>;
      };
      wishlists: {
        Row: Wishlist;
        Insert: WishlistInsert;
        Update: Partial<WishlistInsert>;
      };
      faq: {
        Row: Faq;
        Insert: FaqInsert;
        Update: FaqUpdate;
      };
      promo_codes: {
        Row: PromoCode;
        Insert: PromoCodeInsert;
        Update: PromoCodeUpdate;
      };
      messages: {
        Row: Message;
        Insert: Omit<Message, 'id' | 'created_at'>;
        Update: Partial<Omit<Message, 'id' | 'created_at'>>;
      };
      telegram_sessions: {
        Row: TelegramSession;
        Insert: Omit<TelegramSession, 'updated_at'>;
        Update: Partial<Omit<TelegramSession, 'chat_id'>>;
      };
      agent_findings: {
        Row: AgentFinding;
        Insert: Omit<AgentFinding, 'id' | 'created_at'>;
        Update: Partial<Omit<AgentFinding, 'id' | 'created_at'>>;
      };
      agent_discussions: {
        Row: AgentDiscussion;
        Insert: Omit<AgentDiscussion, 'id' | 'created_at'>;
        Update: Partial<Omit<AgentDiscussion, 'id' | 'created_at'>>;
      };
      order_status_history: {
        Row: OrderStatusHistory;
        Insert: Omit<OrderStatusHistory, 'id' | 'created_at'>;
        Update: Partial<Omit<OrderStatusHistory, 'id' | 'created_at'>>;
      };
      loyalty_points: {
        Row: LoyaltyPoints;
        Insert: Omit<LoyaltyPoints, 'id' | 'updated_at'>;
        Update: Partial<Omit<LoyaltyPoints, 'id' | 'chat_id'>>;
      };
      favorites: {
        Row: Favorite;
        Insert: Omit<Favorite, 'id' | 'created_at'>;
        Update: never;
      };
      client_prefs: {
        Row: ClientPrefs;
        Insert: Omit<ClientPrefs, 'updated_at'>;
        Update: Partial<Omit<ClientPrefs, 'chat_id'>>;
      };
      audit_log: {
        Row: AuditLog;
        Insert: Omit<AuditLog, 'id' | 'created_at'>;
        Update: never;
      };
      model_busy_dates: {
        Row: ModelBusyDate;
        Insert: Omit<ModelBusyDate, 'id' | 'created_at'>;
        Update: Partial<Omit<ModelBusyDate, 'id' | 'created_at'>>;
      };
      social_posts: {
        Row: SocialPost;
        Insert: Omit<SocialPost, 'id' | 'created_at'>;
        Update: Partial<Omit<SocialPost, 'id' | 'created_at'>>;
      };
      model_availability: {
        Row: ModelAvailability;
        Insert: Omit<ModelAvailability, 'id' | 'created_at' | 'updated_at'>;
        Update: Partial<Omit<ModelAvailability, 'id' | 'model_id'>>;
      };
      model_availability_schedule: {
        Row: ModelAvailabilitySchedule;
        Insert: Omit<ModelAvailabilitySchedule, 'id' | 'created_at'>;
        Update: Partial<Omit<ModelAvailabilitySchedule, 'id' | 'created_at'>>;
      };
      scheduled_broadcasts: {
        Row: ScheduledBroadcast;
        Insert: Omit<ScheduledBroadcast, 'id' | 'created_at' | 'sent_count' | 'error_count'>;
        Update: Partial<Omit<ScheduledBroadcast, 'id' | 'created_at'>>;
      };
      bot_broadcasts: {
        Row: BotBroadcast;
        Insert: Omit<BotBroadcast, 'id' | 'started_at'>;
        Update: Partial<Omit<BotBroadcast, 'id' | 'started_at'>>;
      };
      support_messages: {
        Row: SupportMessage;
        Insert: Omit<SupportMessage, 'id' | 'created_at'>;
        Update: Partial<Omit<SupportMessage, 'id' | 'created_at'>>;
      };
      quick_bookings: {
        Row: QuickBooking;
        Insert: Omit<QuickBooking, 'id' | 'created_at'>;
        Update: Partial<Omit<QuickBooking, 'id' | 'created_at'>>;
      };
    };
    Views: Record<string, never>;
    Functions: Record<string, never>;
    Enums: Record<string, never>;
  };
}
