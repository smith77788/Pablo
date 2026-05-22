from aiogram.filters.callback_data import CallbackData


class BotCb(CallbackData, prefix="bot"):
    action: str   # list | select | delete | confirm_delete
    bot_id: int = 0
    page: int = 0


class EditCb(CallbackData, prefix="edit"):
    action: str   # menu | name | name_lang | desc | desc_lang | short | short_lang | photo
    bot_id: int


class AudCb(CallbackData, prefix="aud"):
    action: str   # menu | refresh | compare | pick_b
    bot_id: int
    target_id: int = 0  # second bot for compare


class WebhookCb(CallbackData, prefix="wh"):
    action: str   # menu | set | delete
    bot_id: int


class BroadcastCb(CallbackData, prefix="bc"):
    action: str   # menu | compose | confirm | cancel | status
    bot_id: int
    broadcast_id: int = 0
