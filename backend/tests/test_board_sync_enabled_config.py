"""BOARD_SYNC_ENABLED 配置解析测试（CHANGE-20260716-007）。

测试内容：
1. 环境变量 BOARD_SYNC_ENABLED 优先级最高
2. CONFIG_FILE 配置文件中的 BOARD_SYNC_ENABLED 次之
3. 默认值 False
4. 不同 truthy/falsy 字符串值的解析
5. PR #77 §三.5：配置文件字符串 "false" 不被 bool("false") 误判为 True
6. PR #77 §三.5：非法值启动失败（RuntimeError）

测试策略：
- 使用 monkeypatch 修改环境变量
- 直接调用 _resolve_board_sync_enabled 函数
- monkeypatch app.config._load_py_config 模拟配置文件值
"""
from __future__ import annotations

import pytest

from app import config as config_module
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


# =============================================================================
# PR #77 §三.5 收口：配置文件严格解析（修复 bool("false")=True 的 bug）
# =============================================================================


class TestResolveBoardSyncEnabledConfigFileStrict:
    """配置文件 BOARD_SYNC_ENABLED 严格解析测试。

    旧行为 `return bool(file_val)` 中 `bool("false")` 为 True（非空字符串 truthy），
    导致配置文件写 "false" 实际启用同步。PR #77 收口改为显式 truthy/falsy 集合解析。
    """

    def test_config_file_string_false_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配置文件字符串 "false" 必须解析为 False（P1 bug 修复）。"""
        monkeypatch.delenv("BOARD_SYNC_ENABLED", raising=False)
        monkeypatch.setattr(
            config_module, "_load_py_config", lambda: {"BOARD_SYNC_ENABLED": "false"}
        )
        assert _resolve_board_sync_enabled() is False

    def test_config_file_string_true_returns_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配置文件字符串 "true" 必须解析为 True。"""
        monkeypatch.delenv("BOARD_SYNC_ENABLED", raising=False)
        monkeypatch.setattr(
            config_module, "_load_py_config", lambda: {"BOARD_SYNC_ENABLED": "true"}
        )
        assert _resolve_board_sync_enabled() is True

    def test_config_file_string_0_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配置文件字符串 "0" 必须解析为 False。"""
        monkeypatch.delenv("BOARD_SYNC_ENABLED", raising=False)
        monkeypatch.setattr(
            config_module, "_load_py_config", lambda: {"BOARD_SYNC_ENABLED": "0"}
        )
        assert _resolve_board_sync_enabled() is False

    def test_config_file_string_1_returns_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配置文件字符串 "1" 必须解析为 True。"""
        monkeypatch.delenv("BOARD_SYNC_ENABLED", raising=False)
        monkeypatch.setattr(
            config_module, "_load_py_config", lambda: {"BOARD_SYNC_ENABLED": "1"}
        )
        assert _resolve_board_sync_enabled() is True

    def test_config_file_string_yes_returns_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配置文件字符串 "yes" 必须解析为 True。"""
        monkeypatch.delenv("BOARD_SYNC_ENABLED", raising=False)
        monkeypatch.setattr(
            config_module, "_load_py_config", lambda: {"BOARD_SYNC_ENABLED": "yes"}
        )
        assert _resolve_board_sync_enabled() is True

    def test_config_file_string_no_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配置文件字符串 "no" 必须解析为 False。"""
        monkeypatch.delenv("BOARD_SYNC_ENABLED", raising=False)
        monkeypatch.setattr(
            config_module, "_load_py_config", lambda: {"BOARD_SYNC_ENABLED": "no"}
        )
        assert _resolve_board_sync_enabled() is False

    def test_config_file_string_on_returns_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配置文件字符串 "on" 必须解析为 True。"""
        monkeypatch.delenv("BOARD_SYNC_ENABLED", raising=False)
        monkeypatch.setattr(
            config_module, "_load_py_config", lambda: {"BOARD_SYNC_ENABLED": "on"}
        )
        assert _resolve_board_sync_enabled() is True

    def test_config_file_string_off_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配置文件字符串 "off" 必须解析为 False。"""
        monkeypatch.delenv("BOARD_SYNC_ENABLED", raising=False)
        monkeypatch.setattr(
            config_module, "_load_py_config", lambda: {"BOARD_SYNC_ENABLED": "off"}
        )
        assert _resolve_board_sync_enabled() is False

    def test_config_file_string_empty_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配置文件空字符串应解析为 False。"""
        monkeypatch.delenv("BOARD_SYNC_ENABLED", raising=False)
        monkeypatch.setattr(
            config_module, "_load_py_config", lambda: {"BOARD_SYNC_ENABLED": ""}
        )
        assert _resolve_board_sync_enabled() is False

    def test_config_file_string_uppercase_false_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配置文件字符串大小写不敏感：'FALSE' 必须解析为 False。"""
        monkeypatch.delenv("BOARD_SYNC_ENABLED", raising=False)
        monkeypatch.setattr(
            config_module, "_load_py_config", lambda: {"BOARD_SYNC_ENABLED": "FALSE"}
        )
        assert _resolve_board_sync_enabled() is False

    def test_config_file_string_with_spaces_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配置文件字符串带空格应 strip 后解析：'  false  ' → False。"""
        monkeypatch.delenv("BOARD_SYNC_ENABLED", raising=False)
        monkeypatch.setattr(
            config_module,
            "_load_py_config",
            lambda: {"BOARD_SYNC_ENABLED": "  false  "},
        )
        assert _resolve_board_sync_enabled() is False

    def test_config_file_bool_true_returns_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配置文件 bool True 直接返回 True。"""
        monkeypatch.delenv("BOARD_SYNC_ENABLED", raising=False)
        monkeypatch.setattr(
            config_module, "_load_py_config", lambda: {"BOARD_SYNC_ENABLED": True}
        )
        assert _resolve_board_sync_enabled() is True

    def test_config_file_bool_false_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配置文件 bool False 直接返回 False。"""
        monkeypatch.delenv("BOARD_SYNC_ENABLED", raising=False)
        monkeypatch.setattr(
            config_module, "_load_py_config", lambda: {"BOARD_SYNC_ENABLED": False}
        )
        assert _resolve_board_sync_enabled() is False

    def test_config_file_invalid_string_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配置文件非法字符串必须抛 RuntimeError（fail-fast，不静默 True）。"""
        monkeypatch.delenv("BOARD_SYNC_ENABLED", raising=False)
        monkeypatch.setattr(
            config_module,
            "_load_py_config",
            lambda: {"BOARD_SYNC_ENABLED": "maybe"},
        )
        with pytest.raises(RuntimeError, match="BOARD_SYNC_ENABLED 配置文件值非法"):
            _resolve_board_sync_enabled()

    def test_config_file_invalid_type_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配置文件非 bool/str 类型必须抛 RuntimeError。"""
        monkeypatch.delenv("BOARD_SYNC_ENABLED", raising=False)
        monkeypatch.setattr(
            config_module,
            "_load_py_config",
            lambda: {"BOARD_SYNC_ENABLED": 123},
        )
        with pytest.raises(RuntimeError, match="BOARD_SYNC_ENABLED 配置文件类型非法"):
            _resolve_board_sync_enabled()

    def test_env_var_invalid_string_raises_runtime_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """环境变量非法字符串必须抛 RuntimeError（fail-fast）。"""
        monkeypatch.setenv("BOARD_SYNC_ENABLED", "maybe")
        with pytest.raises(RuntimeError, match="BOARD_SYNC_ENABLED 环境变量值非法"):
            _resolve_board_sync_enabled()

    def test_env_var_takes_precedence_over_invalid_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """环境变量优先级高于配置文件：环境变量合法时应跳过配置文件校验。"""
        monkeypatch.setenv("BOARD_SYNC_ENABLED", "true")
        # 即使配置文件值非法，环境变量优先，不应抛异常
        monkeypatch.setattr(
            config_module,
            "_load_py_config",
            lambda: {"BOARD_SYNC_ENABLED": "maybe"},
        )
        assert _resolve_board_sync_enabled() is True
