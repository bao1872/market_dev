"""跨 domain 共享的纯数学原语（NO_FORMULA_CHANGE）。

AUCTION-V3.2 §22：Review 的 HHI owner 与其 domain 强耦合，因此按
「EXTRACT_TO_SHARED」路径提取到本包；公式逐字不变，并由 Review parity test 证明
before == after。Auction 与 Review 均只依赖本包，互不调用对方私有逻辑（INV-03）。
"""
