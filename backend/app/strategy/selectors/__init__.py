"""选股策略运行时包 - selector kind 策略实现。

提供：
- DSASelector: DSA 方向稳定性选股策略（基于 features/ 算法）
"""

from __future__ import annotations

from app.strategy.selectors.dsa_selector import DSASelector

__all__ = ["DSASelector"]
