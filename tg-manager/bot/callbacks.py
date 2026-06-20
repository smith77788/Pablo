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
    deal_id: int = 0


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
    chan_id: int = 0  # channel/group ID from managed_channels
    acc_id: int = 0  # account to use for Telethon edits
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
    cluster_name: Optional[str] = None


class AccCb(CallbackData, prefix="acc"):
    action: str
    acc_id: int = 0
    chat_id: int = 0
    page: int = 0


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
    tpl_id: int = 0  # 0 = preset, >0 = user template id
    bot_id: int = 0  # target managed bot
    preset_key: Optional[str] = None  # 'asset_type__preset_id' for library presets


class LibCb(CallbackData, prefix="lib"):
    action: str
    asset_type: Optional[str] = None
    preset_key: Optional[str] = (
        None  # 'channel__news_channel'  (__ avoids aiogram3 separator clash)
    )
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


class GiftTransferCb(CallbackData, prefix="gt"):
    action: str
    id: Optional[str] = None
    page: int = 0


class StrikeCb(CallbackData, prefix="strk"):
    action: str
    page: int = 0


class TaskCb(CallbackData, prefix="tsk"):
    action: str  # "list", "cancel", "cancel_all"
    task_id: Optional[str] = None


class TopoCb(CallbackData, prefix="topo"):
    action: str  # "menu", "overview", "acc_view", "chan_view"
    acc_id: int = 0
    chan_id: int = 0
    page: int = 0


class PackCb(CallbackData, prefix="ppk"):
    action: str  # "menu","create","view","seed","promote","mirror","delete","confirm_delete"
    pack_id: int = 0
    page: int = 0


class BotAdminCb(CallbackData, prefix="badm"):
    action: str  # "panel","edit_reply","list_replies","stats","sync_mirrors"
    bot_id: int = 0
    reply_id: int = 0


class ApprovalCb(CallbackData, prefix="appr"):
    action: str  # "confirm", "cancel"
    op_id: int = 0


class WorkspaceCb(CallbackData, prefix="ws"):
    action: str  # menu, create, view, invite, join, members, leave
    ws_id: int = 0
    page: int = 0


class QuickPostCb(CallbackData, prefix="qp"):
    action: str  # start, cancel, toggle, page, sel_all, desel_all, chans_done,
    # back_to_text, back_to_chans, back_to_timing, timing, publish, save_template
    val: int = 0  # channel_id for toggle; delay_s for timing
    page: int = 0


class ErrorReportCb(CallbackData, prefix="err"):
    action: str  # "start", "cancel"
    report_id: int = 0


class EcoCb(CallbackData, prefix="eco"):
    action: str
    eco_id: int = 0
    page: int = 0


class EcoPickCb(CallbackData, prefix="ecopick"):
    """Select ecosystem object for attach/detach actions."""

    action: str  # "list" | "add"
    object_type: str  # "channel" | "group" | "bot" | "account"
    object_id: int = 0
    eco_id: int = 0


class ResourceActCb(CallbackData, prefix="ract"):
    """Resource Activity Engine callback payload."""

    action: str
    session_id: int = 0
    account_id: int = 0
    page: int = 0


class IntentCb(CallbackData, prefix="intent"):
    """Intent Engine callback payload."""

    action: str  # menu | new | preset | plan | strategy | confirm | manual | history | detail | cancel
    intent_id: int = 0
    value: Optional[str] = None  # intent_type for preset, strategy name for strategy


class InfraHCCb(CallbackData, prefix="ihc"):
    """EPOCH VI: Infrastructure Health Center."""

    action: str  # menu | anomalies | recoveries | run_recovery | health_trend | copilot | resolve_anomaly | back
    item_id: int = 0  # anomaly_id, recovery_id, alert_id
    page: int = 0


class RegCb(CallbackData, prefix="rc"):
    """Registration / creation date checker + full entity analyzer."""

    action: str  # menu | start | exact | history | cancel | analyze | page | export | follow_toggle
    entity_id: int = 0
    entity_type: Optional[str] = None  # user | bot | channel | supergroup | group
    page: int = 0          # history page OR analyzer tab (0=overview,1=stats,2=content,3=network,4=seo,5=admins)


class BotCustomizeCb(CallbackData, prefix="btcz"):
    action: str  # "apply"
    bot_id: int = 0


class PromoCb(CallbackData, prefix="promo"):
    """Bot Promotion Platform — orders, warehouse, panels, logs."""

    action: str  # menu|orders|new_order|order_detail|order_cancel|order_delete
                 # warehouse|bot_detail|bot_delete|bot_add
                 # panels|panel_add|panel_detail|panel_delete|panel_check
                 # topcheck|logs|logs_filter|back
    item_id: int = 0   # order_id | bot_id | panel_id
    page: int = 0
    value: Optional[str] = None  # status filter, log level


class SelfPromoCb(CallbackData, prefix="sp"):
    """Self-promotion system — BotMother рекламирует себя через каналы и DM."""

    action: str  # menu|list|view|add_ask|add_style|add_skip_cta|add_skip_url
                 # del_confirm|del_do
                 # launch_channel|run_confirm|run_now
                 # share_link|history
    item_id: int = 0   # template_id | run_id
    page: int = 0
    style: str = ""    # 'direct' | 'native'


class GhostCb(CallbackData, prefix="ghst"):
    """Ghost Engine — autonomous background presence for TG accounts."""

    action: str        # menu|add|pick_acc|view|toggle|personality|set_p|hours|set_hours|cap|set_cap|logs|del|del_confirm
    profile_id: int = 0
    account_id: int = 0
    page: int = 0
    extra: str = ""    # personality slug or hours/cap value


class ContentMeshCb(CallbackData, prefix="cmesh"):
    """Content Mesh — automated content distribution network."""

    action: str        # menu|create|view|toggle|set_source|pick_account|targets|add_target|del_target|settings|logs|del|del_confirm
    mesh_id: int = 0
    page: int = 0
    extra: str = ""    # account_id (str) or target_id (str)


class CloneAdaptCb(CallbackData, prefix="cla"):
    """Clone & Adapt — clone bot profiles to multiple target bots."""

    action: str        # menu|start|source|toggle_field|suffix_ask|no_suffix|toggle_target|targets_all|targets_none|targets_page|preview|run
    bot_id: int = 0    # source bot id
    page: int = 0
    extra: str = ""    # field name or target bot id (str)


class AutoFunnelCb(CallbackData, prefix="afn"):
    """Auto-Funnel — automated message sequences for bot audience segments."""

    action: str        # menu|create|view|toggle|steps|add_step|del_step|step_no_btn|launch|launch_confirm|stats|del|del_confirm|pick_bot|pick_segment
    funnel_id: int = 0
    page: int = 0
    extra: str = ""    # bot_id or segment or step_id


class PhysicsCb(CallbackData, prefix="phys"):
    """Physics Engine — account risk scores and safety envelopes."""

    action: str         # menu|detail
    account_id: int = 0
    page: int = 0


class GraphCb(CallbackData, prefix="grph"):
    """Social Graph Engine — audience overlap and channel relationship map."""

    action: str         # menu|overlaps|my_nodes
    page: int = 0


class ApiHubCb(CallbackData, prefix="apih"):
    """Compute API Hub — per-user API key management."""

    action: str         # menu|create|revoke|revoke_confirm|docs
    item_id: int = 0


class ComplianceCb(CallbackData, prefix="cmpl"):
    """Compliance Engine — cryptographic audit trail."""

    action: str         # menu|history|export
    page: int = 0



