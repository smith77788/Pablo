from typing import Optional
from aiogram.filters.callback_data import CallbackData


class BotCb(CallbackData, prefix="bot"):
    action: str
    bot_id: int = 0
    page: int = 0


class EditCb(CallbackData, prefix="edit"):
    action: str
    bot_id: int


class AudCb(CallbackData, prefix="aud"):
    action: str
    bot_id: int
    target_id: int = 0


class WebhookCb(CallbackData, prefix="wh"):
    action: str
    bot_id: int


class BroadcastCb(CallbackData, prefix="bc"):
    action: str
    bot_id: int
    broadcast_id: int = 0
    lang: Optional[str] = None


class BulkCb(CallbackData, prefix="bulk"):
    action: str


class CommandsCb(CallbackData, prefix="cmd"):
    action: str
    bot_id: int


class TemplateCb(CallbackData, prefix="tpl"):
    action: str
    bot_id: int = 0
    template_id: int = 0


class ScheduleCb(CallbackData, prefix="sch"):
    action: str
    bot_id: int
    schedule_id: int = 0


class MultigeoCb(CallbackData, prefix="mg"):
    action: str
    bot_id: int
    lang: Optional[str] = None


class AutoReplyCb(CallbackData, prefix="ar"):
    action: str
    bot_id: int
    reply_id: int = 0
    target_bot_id: int = 0


class RelayCb(CallbackData, prefix="rl"):
    action: str
    bot_id: int
    session_id: int = 0
    template_id: int = 0


class FunnelCb(CallbackData, prefix="fn"):
    action: str
    bot_id: int = 0
    funnel_id: int = 0
    step: int = 0
    target_bot_id: int = 0


class StatsCb(CallbackData, prefix="st"):
    action: str
    bot_id: int


class NoteCb(CallbackData, prefix="note"):
    action: str
    bot_id: int


class SwarmCb(CallbackData, prefix="sw"):
    action: str
    bot_id: int = 0


class CrmCb(CallbackData, prefix="crm"):
    action: str
    bot_id: int = 0
    user_id: int = 0
    tag: Optional[str] = None

class AutoCb(CallbackData, prefix="au"):
    action: str
    bot_id: int = 0
    rule_id: int = 0

class ExperimentCb(CallbackData, prefix="exp"):
    action: str
    bot_id: int = 0
    exp_id: int = 0
    variant_id: int = 0

class DeepLinkCb(CallbackData, prefix="dl"):
    action: str
    bot_id: int = 0
    link_id: int = 0

class EngageCb(CallbackData, prefix="eng"):
    action: str
    bot_id: int = 0


class SeoCb(CallbackData, prefix="seo"):
    action: str
    bot_id: int = 0
    chan_id: int = 0          # channel/group ID from managed_channels
    acc_id: int = 0           # account to use for Telethon edits
    etype: Optional[str] = None  # 'bot' | 'channel' | 'group'


class NetworkCb(CallbackData, prefix="net"):
    action: str
    bot_id: int = 0
    page: int = 0


class ClusterCb(CallbackData, prefix="cl"):
    action: str
    cluster: Optional[str] = None
    bot_id: int = 0


class SubCb(CallbackData, prefix="sub"):
    action: str
    plan: Optional[str] = None
    months: int = 1
    currency: Optional[str] = None


class AiCb(CallbackData, prefix="ai"):
    action: str
    bot_id: int = 0
    memory_id: int = 0


class NetBcCb(CallbackData, prefix="nbc"):
    action: str
    bot_id: int = 0
    segment: str = "all"
    lang: Optional[str] = None


class AccCb(CallbackData, prefix="acc"):
    action: str
    acc_id: int = 0
    chat_id: int = 0


class RankCb(CallbackData, prefix="rank"):
    action: str
    bot_id: int = 0
    keyword_id: int = 0


class ChanCb(CallbackData, prefix="chan"):
    action: str
    acc_id: int = 0
    channel_id: int = 0
    page: int = 0


class ContactInvCb(CallbackData, prefix="cinv"):
    action: str
    acc_id: int = 0
    channel_id: int = 0
    page: int = 0


class RefCb(CallbackData, prefix="ref"):
    action: str  # "menu", "leaderboard"


class BmCb(CallbackData, prefix="bm"):
    action: str
    sub: Optional[str] = None
    page: int = 0
    op_id: int = 0


class AssetTplCb(CallbackData, prefix="atpl"):
    action: str
    tpl_id: int = 0
    asset_type: Optional[str] = None


class GroupFCb(CallbackData, prefix="grpf"):
    action: str
    acc_id: int = 0
    group_id: int = 0
    page: int = 0


class MassOpCb(CallbackData, prefix="mop"):
    action: str
    op_type: Optional[str] = None
    op_id: int = 0
    page: int = 0


class BotFactCb(CallbackData, prefix="btf"):
    action: str
    bot_id: int = 0
    page: int = 0


class ChanFactCb(CallbackData, prefix="chanf"):
    action: str
    acc_id: int = 0
    channel_id: int = 0
    page: int = 0


class MassPubCb(CallbackData, prefix="mpub"):
    action: str
    target_type: Optional[str] = None
    target_id: int = 0
    page: int = 0


class CompCb(CallbackData, prefix="comp"):
    action: str
    comp_id: int = 0
    page: int = 0


class VisCb(CallbackData, prefix="vis"):
    action: str
    bot_id: int = 0
    kw_id: int = 0
    page: int = 0


class HealthCb(CallbackData, prefix="hlth"):
    action: str
    acc_id: int = 0
    page: int = 0


class ProxyCb(CallbackData, prefix="prx"):
    action: str
    proxy_id: int = 0
    page: int = 0


class ClustMCb(CallbackData, prefix="clm"):
    action: str
    cluster_name: Optional[str] = None
    bot_id: int = 0
    page: int = 0


class GeoPresenceCb(CallbackData, prefix="gp"):
    action: str
    plan_id: int = 0
    page: int = 0
    item: Optional[str] = None


class TplBotApplyCb(CallbackData, prefix="tba"):
    tpl_id: int = 0           # 0 = preset, >0 = user template id
    bot_id: int = 0           # target managed bot
    preset_key: Optional[str] = None   # 'asset_type__preset_id' for library presets


class LibCb(CallbackData, prefix="lib"):
    action: str
    asset_type: Optional[str] = None
    preset_key: Optional[str] = None  # 'channel__news_channel'  (__ avoids aiogram3 separator clash)
    page: int = 0


class ParserCb(CallbackData, prefix="prs"):
    action: str
    run_id: int = 0
    source_id: int = 0
    page: int = 0


class WarmupCb(CallbackData, prefix="wu"):
    action: str
    account_id: int = 0
    plan_id: int = 0
    session_id: int = 0
    page: int = 0


class InfraCb(CallbackData, prefix="infra"):
    action: str
    account_id: int = 0
    page: int = 0


class CleanerCb(CallbackData, prefix="cln"):
    action: str
    account_id: int = 0


class DmCb(CallbackData, prefix="dm"):
    action: str
    campaign_id: int = 0
    page: int = 0


class StrikeCb(CallbackData, prefix="strk"):
    action: str
    page: int = 0


class TaskCb(CallbackData, prefix="tsk"):
    action: str          # "list", "cancel", "cancel_all"
    task_id: Optional[str] = None


class TopoCb(CallbackData, prefix="topo"):
    action: str       # "menu", "overview", "acc_view", "chan_view"
    acc_id: int = 0
    chan_id: int = 0
    page: int = 0


class PackCb(CallbackData, prefix="ppk"):
    action: str        # "menu","create","view","seed","promote","mirror","delete","confirm_delete"
    pack_id: int = 0
    page: int = 0


class BotAdminCb(CallbackData, prefix="badm"):
    action: str        # "panel","edit_reply","list_replies","stats","sync_mirrors"
    bot_id: int = 0
    reply_id: int = 0


class ApprovalCb(CallbackData, prefix="appr"):
    action: str   # "confirm", "cancel"
    op_id: int = 0


class WorkspaceCb(CallbackData, prefix="ws"):
    action: str   # menu, create, view, invite, join, members, leave
    ws_id: int = 0
    page: int = 0


class QuickPostCb(CallbackData, prefix="qp"):
    action: str   # start, cancel, toggle, page, sel_all, desel_all, chans_done,
                  # back_to_text, back_to_chans, back_to_timing, timing, publish, save_template
    val: int = 0  # channel_id for toggle; delay_s for timing
    page: int = 0


class ErrorReportCb(CallbackData, prefix="err"):
    action: str   # "start", "cancel"
    report_id: int = 0


class EcoCb(CallbackData, prefix="eco"):
    action: str
    eco_id: int = 0
    page: int = 0


class EcoPickCb(CallbackData, prefix="ecopick"):
    """Выбор экосистемы для добавления объекта (из фабрик)."""
    action: str        # "list" | "add"
    object_type: str   # "channel" | "group" | "bot" | "account"
    object_id: int = 0
    eco_id: int = 0


class ResourceActCb(CallbackData, prefix="ract"):
    """Resource Activity Engine — активность в собственных ресурсах."""
    action: str
    session_id: int = 0
    account_id: int = 0
    page: int = 0


