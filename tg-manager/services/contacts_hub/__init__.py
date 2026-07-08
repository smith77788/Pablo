from .repository import (
    get_contacts, get_contact, upsert_contact, update_contact, delete_contact,
    get_contact_groups, add_contact_to_group, remove_contact_from_group,
    get_contact_stats, log_contact_history, log_sync,
)
from .merge_engine import find_duplicates, auto_merge, manual_merge
from .search_engine import search_contacts
from .stats_engine import get_full_stats, get_account_stats
from .sync_service import sync_account, sync_all_accounts
from .versioning_engine import (
    create_version, get_versions, get_version_detail, rollback_to_version, get_contact_snapshot,
)
from .relationship_engine import compute_relationships, get_relationships, get_graph_stats
from .trust_engine import (
    compute_trust_score, compute_merge_confidence, detect_smart_duplicates,
    update_trust_scores, get_conflicts, resolve_conflict,
)
from .crm_engine import (
    get_crm_data, upsert_crm, log_crm_activity, get_crm_activity,
    get_upcoming_reminders, get_crm_stats, get_crm_overdue,
)
from .smart_tags_engine import (
    apply_smart_tags, get_smart_tags, get_smart_tag_rules,
    create_smart_tag_rule, delete_smart_tag_rule, toggle_smart_tag_rule,
)
from .bulk_ops_engine import (
    bulk_tag, bulk_untag, bulk_set_favorite, bulk_delete,
    bulk_add_to_group, bulk_remove_from_group,
    create_group, update_group, delete_group,
)
from .export_engine import export_csv, export_vcf, export_json
from .identity_engine import build_identity_graph, get_identity_graph, get_last_active
