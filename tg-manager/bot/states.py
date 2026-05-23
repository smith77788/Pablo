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
