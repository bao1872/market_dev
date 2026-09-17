"""Non-kernel view primitives shared by canonical consumers.

This module separates DTO builders and temporal projection helpers from the
algorithm adapter registry, so read-model services do not depend back on the
adapter composition module.
"""

from app.services.node_cluster_engine import (
    NodeClusterProfileResult,
    build_node_regions,
    build_price_state,
    compute_node_regions_hash,
    derive_state_for_price,
    profile_to_dict,
)
from app.services.temporal_feature_service import (
    _compute_daily_context,
    _compute_derived_relation,
    _compute_m15_response,
)

__all__ = [
    "NodeClusterProfileResult",
    "build_node_regions",
    "build_price_state",
    "compute_node_regions_hash",
    "derive_state_for_price",
    "profile_to_dict",
    "_compute_daily_context",
    "_compute_derived_relation",
    "_compute_m15_response",
]
