from aiogram.fsm.state import State, StatesGroup


class AddBot(StatesGroup):
    waiting_token = State()


class EditProfile(StatesGroup):
    waiting_name = State()
    waiting_name_lang = State()
    waiting_localized_name = State()
    waiting_desc = State()
    waiting_desc_lang = State()
    waiting_localized_desc = State()
    waiting_short = State()
    waiting_short_lang = State()
    waiting_localized_short = State()
    waiting_photo = State()


class SetWebhook(StatesGroup):
    waiting_url = State()


class Broadcast(StatesGroup):
    waiting_message = State()
    confirming = State()
    waiting_button_text = State()
    waiting_button_url = State()


class Compare(StatesGroup):
    waiting_second_bot = State()


class BulkEdit(StatesGroup):
    waiting_name = State()
    waiting_name_lang = State()
    waiting_localized_name = State()
    waiting_desc = State()
    waiting_desc_lang = State()
    waiting_localized_desc = State()
    waiting_short = State()
    waiting_short_lang = State()
    waiting_localized_short = State()
    waiting_commands = State()
    waiting_commands_lang = State()
    waiting_localized_commands = State()


class SetCommands(StatesGroup):
    waiting_add = State()
    waiting_commands = State()


class MultigeoEdit(StatesGroup):
    waiting_name = State()
    waiting_short = State()
    waiting_desc = State()


class AddTemplate(StatesGroup):
    waiting_name = State()
    waiting_text = State()


class ScheduleBroadcast(StatesGroup):
    waiting_message = State()
    waiting_datetime = State()


class ImportBots(StatesGroup):
    waiting_tokens = State()


class AddAutoReply(StatesGroup):
    choosing_trigger = State()
    waiting_keyword = State()
    waiting_text = State()


class CreateFunnel(StatesGroup):
    waiting_name = State()
    waiting_trigger = State()
    waiting_keyword = State()
    waiting_step_text = State()
    waiting_step_delay = State()


class UpdateToken(StatesGroup):
    waiting_token = State()


class SendToUser(StatesGroup):
    waiting_user_id = State()
    waiting_message = State()


class FunnelBroadcast(StatesGroup):
    waiting_message = State()


class CreateDeepLink(StatesGroup):
    waiting_name = State()
    waiting_param = State()


class ReactivateBroadcast(StatesGroup):
    waiting_message = State()


class NetworkBroadcast(StatesGroup):
    waiting_message = State()
    confirming = State()


class CloneSettings(StatesGroup):
    picking_dest = State()


class SetRoutingWeight(StatesGroup):
    waiting_weight = State()


class AssignCluster(StatesGroup):
    waiting_name = State()


class AiChat(StatesGroup):
    chatting = State()


class NetworkBroadcastV2(StatesGroup):
    choosing_target = State()
    choosing_segment = State()
    waiting_message = State()
    confirming = State()


class AddKeyword(StatesGroup):
    waiting_keyword = State()


class CreateChannelFSM(StatesGroup):
    waiting_title = State()
    waiting_about = State()
    confirming = State()


class JoinChannelFSM(StatesGroup):
    waiting_invite = State()


class PostToChannelFSM(StatesGroup):
    waiting_channel_id = State()
    waiting_text = State()


class EditChannelFSM(StatesGroup):
    waiting_value = State()


class InviteUsersFSM(StatesGroup):
    waiting_channel_id = State()
    waiting_usernames = State()


class UpdateProfileFSM(StatesGroup):
    waiting_value = State()


class CreateBotFSM(StatesGroup):
    waiting_count = State()
    waiting_name = State()
    waiting_username = State()


class SendReactionFSM(StatesGroup):
    waiting_msg_id = State()
    choosing_emoji = State()


class ReportFSM(StatesGroup):
    waiting_peer = State()
    choosing_reason = State()
    waiting_comment = State()


class BulkReportFSM(StatesGroup):
    waiting_peer = State()        # одиночный target
    waiting_peers_batch = State() # список targets (batch mode)
    choosing_reason = State()
    selecting_accounts = State()


class BulkCreateFSM(StatesGroup):
    waiting_title = State()
    waiting_about = State()
    choosing_type = State()
    waiting_count = State()
    choosing_name_mode = State()
    confirming = State()


class BulkPostChansFSM(StatesGroup):
    choosing_channels = State()
    waiting_text = State()


class BulkDmFSM(StatesGroup):
    waiting_usernames = State()
    waiting_text = State()


class BulkChanFSM(StatesGroup):
    waiting_value = State()   # ожидаем username-шаблон или текст описания


class SeoFSM(StatesGroup):
    waiting_feedback   = State()   # ждём правки к AI-предложению
    waiting_username   = State()   # ждём желаемый username от пользователя


class MyChannelsFSM(StatesGroup):
    choosing_account = State()
    browsing = State()
    posting = State()


class PaymentSettingsFSM(StatesGroup):
    waiting_value = State()


class ContactInviteFSM(StatesGroup):
    entering_channel = State()
    choosing_accounts = State()
    confirming = State()


class AssetTemplateFSM(StatesGroup):
    choosing_type = State()
    waiting_name = State()
    waiting_json = State()    # ввод параметров (name, desc, etc.)
    confirming = State()


class CreateGroupFSM(StatesGroup):
    choosing_account = State()
    waiting_title = State()
    waiting_about = State()
    choosing_type = State()   # supergroup or group
    confirming = State()


class AnnounceGroupFSM(StatesGroup):
    choosing_account = State()
    waiting_text = State()
    confirming = State()


class MassPublishFSM(StatesGroup):
    choosing_targets = State()    # выбор каналов/групп
    choosing_selector = State()   # by cluster / by tag / by account / all
    waiting_text = State()        # текст поста
    choosing_timing = State()     # немедленно / с задержкой
    previewing = State()          # предпросмотр перед запуском
    confirming = State()          # финальное подтверждение


class BulkBotEditFSM(StatesGroup):
    choosing_field = State()      # name | desc | short_desc | commands
    waiting_value = State()
    previewing = State()
    confirming = State()


class BotTokenImportFSM(StatesGroup):
    waiting_tokens = State()    # bulk token paste
    reviewing = State()         # review import results


class BotCloneSettingsFSM(StatesGroup):
    choosing_source = State()   # source bot
    choosing_targets = State()  # target bots
    choosing_fields = State()   # what to clone
    confirming = State()


class BotValidateFSM(StatesGroup):
    waiting_tokens = State()    # paste tokens to validate


class ChannelFactoryFSM(StatesGroup):
    choosing_account = State()
    waiting_title = State()
    waiting_about = State()
    waiting_username = State()
    choosing_cluster = State()
    choosing_template = State()
    confirming = State()


class BulkChannelCreateFSM(StatesGroup):
    choosing_account = State()
    waiting_count = State()
    waiting_prefix = State()    # prefix for channel names: "Shop 1", "Shop 2"...
    waiting_about = State()
    confirming = State()


class MassPublishFSM2(StatesGroup):
    choosing_target_type = State()   # all | by_account | by_cluster
    choosing_target = State()        # specific account or cluster
    waiting_text = State()
    choosing_timing = State()        # immediate | delay_5s | delay_30s | delay_60s
    previewing = State()
    confirming = State()


class EditChannelBulkFSM(StatesGroup):
    choosing_field = State()     # title | about | username
    choosing_scope = State()     # all_channels | by_account
    waiting_value = State()
    previewing = State()
    confirming = State()


class AddCompetitorFSM(StatesGroup):
    waiting_username = State()
    waiting_label = State()


class AddKeywordFSM(StatesGroup):
    choosing_bot = State()
    waiting_keyword = State()
    waiting_region = State()   # "ua" | "ru" | "en" | "skip"


class KeywordAlertFSM(StatesGroup):
    choosing_threshold = State()  # позиция-порог для алерта


class AddProxyFSM(StatesGroup):
    waiting_url = State()    # socks5://user:pass@host:port
    waiting_label = State()


class CreateClusterFSM(StatesGroup):
    waiting_name = State()
    waiting_description = State()


class OpPlannerFSM(StatesGroup):
    waiting_text = State()      # текст для mass_publish
    waiting_links = State()     # список ссылок для bulk_join/bulk_leave
    waiting_datetime = State()  # дата и время запуска


class BulkJoinFSM(StatesGroup):
    waiting_links = State()     # ссылки/юзернеймы каналов (по одному на строку)
    choosing_accounts = State() # выбор аккаунтов


class BulkLeaveFSM(StatesGroup):
    waiting_channels = State()  # юзернеймы/ID каналов для выхода (по одному на строку)
    choosing_accounts = State() # выбор аккаунтов


class OpBuilderFSM(StatesGroup):
    choosing_op_type = State()   # тип операции: mass_publish | bulk_join | bulk_leave | bulk_bot_edit
    choosing_targets = State()   # выбор целей (каналы/аккаунты/ссылки)
    entering_params = State()    # ввод дополнительных параметров (текст поста / ссылки)
    confirming = State()         # финальное подтверждение перед записью в operation_queue


class GlobalPresenceFSM(StatesGroup):
    choosing_asset_type = State()
    choosing_template = State()
    entering_name_pattern = State()
    entering_username_pattern = State()
    choosing_geo = State()
    entering_custom_geo = State()
    choosing_accounts = State()
    previewing = State()
    confirming = State()


class DmCampaignFSM(StatesGroup):
    waiting_name       = State()   # название кампании
    waiting_text       = State()   # текст с spintax
    choosing_target    = State()   # выбор типа аудитории (bot_users/crm)
    choosing_bot       = State()   # выбор бота (для bot_users)


class CleanerFSM(StatesGroup):
    choosing_account = State()
    confirm_action   = State()
