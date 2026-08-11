"""Version constants shared by Review compute, history, and publication."""

# review-2.0.0: Review-v2 正式算法/契约版本。
# source-drift correction (825525e) 不改变 P/Q/U/C/V 公式、归一化、filter、
# Discovery 语义或历史 observation 契约，因此版本号保持 review-2.0.0，
# 不创建不必要的 history-series 隔离。
REVIEW_ALGORITHM_VERSION = "review-2.0.0"
