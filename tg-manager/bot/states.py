from aiogram.fsm.state import State, StatesGroup


class AddBot(StatesGroup):
    waiting_token = State()


class EditProfile(StatesGroup):
    waiting_name = State()
    waiting_name_lang = State()        # ask language code first
    waiting_localized_name = State()   # then the name itself
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


class Compare(StatesGroup):
    waiting_second_bot = State()   # user types username or bot_id
