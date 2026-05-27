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
