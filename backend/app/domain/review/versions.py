"""Version constants shared by Review compute, history, and publication."""

# review-2.0.1: [REVIEW-CURRENT-FACT-SOURCE-DRIFT FIX] CURRENT First Pyramid facts
# 来源从 FirstPyramidHistoryDailyState(T) 改为当日正式 stock_core 指针
# (StockFeatureSnapshot.summary_payload.first_pyramid_flat, by source_core_run_id)；
# 历史 baseline 仅取 trade_date < T。其它算法/契约/PRD 语义不变。
REVIEW_ALGORITHM_VERSION = "review-2.0.1"

