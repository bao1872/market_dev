"""Version constants shared by Review compute, history, and publication."""

# review-2.0.1: Current SMC ownership correction (REVIEW-CURRENT-SMC-OWNER /
# MIXED_CONTRACT_BUG).  Current SMC STATE is now owned by Core(T)
# (``structure.current``), independent of History(T) event coverage.  DSA Current
# Core mapping (b71c6981) and segment Current Core mapping (aad1aac0) are also part
# of this algorithm generation.  Does NOT change History-v3 contract, filter
# version, or the immutable event-evidence owner (``structure.events`` = History).
REVIEW_ALGORITHM_VERSION = "review-2.0.1"
