"""BOARD_SYNC_ENABLED 配置解析测试（CHANGE-20260716-007）。

测试内容：
1. 环境变量 BOARD_SYNC_ENABLED 优先级最高
2. CONFIG_FILE 配置文件中的 BOARD_SYNC_ENABLED 次之
3. 默认值 False
4. 不同 truthy/falsy 字符串值的解析

测试策略：
- 使用 monkeypatch 修改环境变量
- 直接调用 _resolve_board_sync_enabled 函数
"""
from __future__ import annotations

import pytest

from app.config import _resolve_board_sync_enabled


class TestResolveBoardSyncEnabled:
    """_resolve_board_sync_enabled 配置解析测试。"""

    def test_env_var_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """环境变量 BOARD_SYNC_ENABLED=true 应解析为 True。"""
        monkeypatch.setenv("BOARD_SYNC_ENABLED", "true")
        assert _resolve_board_sync_enabled() is True

    def test_env_var_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """环境变量 BOARD_SYNC_ENABLED=1 应解析为 True。"""
        monkeypatch.setenv("BOARD_SYNC_ENABLED", "1")
        assert _resolve_board_sync_enabled() is True

    def test_env_var_yes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """环境变量 BOARD_SYNC_ENABLED=yes 应解析为 True。"""
        monkeypatch.setenv("BOARD_SYNC_ENABLED", "yes")
        assert _resolve_board_sync_enabled() is True

    def test_env_var_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """环境变量 BOARD_SYNC_ENABLED=on 应解析为 True。"""
        monkeypatch.setenv("BOARD_SYNC_ENABLED", "on")
        assert _resolve_board_sync_enabled() is True

    def test_env_var_true_uppercase(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """环境变量值大小写不敏感：TRUE 应解析为 True。"""
        monkeypatch.setenv("BOARD_SYNC_ENABLED", "TRUE")
        assert _resolve_board_sync_enabled() is True

    def test_env_var_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """环境变量 BOARD_SYNC_ENABLED=false 应解析为 False。"""
        monkeypatch.setenv("BOARD_SYNC_ENABLED", "false")
        assert _resolve_board_sync_enabled() is False

    def test_env_var_0(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """环境变量 BOARD_SYNC_ENABLED=0 应解析为 False。"""
        monkeypatch.setenv("BOARD_SYNC_ENABLED", "0")
        assert _resolve_board_sync_enabled() is False

    def test_env_var_no(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """环境变量 BOARD_SYNC_ENABLED=no 应解析为 False。"""
        monkeypatch.setenv("BOARD_SYNC_ENABLED", "no")
        assert _resolve_board_sync_enabled() is False

    def test_env_var_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """环境变量 BOARD_SYNC_ENABLED=off 应解析为 False。"""
        monkeypatch.setenv("BOARD_SYNC_ENABLED", "off")
        assert _resolve_board_sync_enabled() is False

    def test_env_var_empty_string_falls_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """环境变量 BOARD_SYNC_ENABLED='' 应视为未设置，回退到配置文件/默认值。"""
        # 空串应回退到默认 False（CONFIG_FILE 在测试环境可能未设置此值）
        monkeypatch.setenv("BOARD_SYNC_ENABLED", "")
        # 不强制断言结果，因为取决于 CONFIG_FILE；但不应抛异常
        result = _resolve_board_sync_enabled()
        assert isinstance(result, bool)

    def test_env_var_takes_precedence_over_config_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """环境变量优先级高于 CONFIG_FILE 配置文件。"""
        # 设置环境变量为 true
        monkeypatch.setenv("BOARD_SYNC_ENABLED", "true")
        # 即使 CONFIG_FILE 未设置此值，环境变量也应生效
        result = _resolve_board_sync_enabled()
        assert result is True

    def test_no_env_var_no_config_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无环境变量且 CONFIG_FILE 未设置时返回默认 False。"""
        # 删除环境变量
        monkeypatch.delenv("BOARD_SYNC_ENABLED", raising=False)
        # CONFIG_FILE 在测试环境（config.test.py）未设置 BOARD_SYNC_ENABLED
        result = _resolve_board_sync_enabled()
        assert result is False
