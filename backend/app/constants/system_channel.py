"""系统级飞书渠道配置 - 从环境变量读取管理员飞书 Platform App。

设计（spec 第四节）：
- 系统级渠道不入库，不暴露前端
- 生产配置中只能有一个 active 管理员飞书目标
- 未配置时返回 None，调用方负责处理（标记 failed 但不影响用户提交）
- 普通用户的飞书渠道（NotificationChannel.user_id NOT NULL）不适用于管理员通知
- 飞书 Webhook 已永久删除（Phase C），统一为 Platform App only

环境变量：
- ADMIN_FEISHU_APP_ID: 飞书应用 ID（必填）
- ADMIN_FEISHU_APP_SECRET: 飞书应用 Secret（必填）
- ADMIN_FEISHU_RECEIVE_ID: 飞书接收者 ID（必填）
- ADMIN_FEISHU_RECEIVE_ID_TYPE: 接收者类型（可选，默认 user_id；可选值 user_id/open_id/union_id）

用法:
    from app.constants.system_channel import get_admin_feishu_config

    config = get_admin_feishu_config()
    if config is None:
        # 未配置，标记 failed 但不影响用户提交
        return
    # config = {"app_id": ..., "app_secret": ..., "receive_id": ..., "receive_id_type": ...}
    adapter.send(dto, config)
"""

from __future__ import annotations

import os
from typing import Any


def get_admin_feishu_config() -> dict[str, Any] | None:
    """返回管理员飞书 Platform App 配置。

    从环境变量读取：
    - ADMIN_FEISHU_APP_ID: 应用 ID（必填，未设置则返回 None）
    - ADMIN_FEISHU_APP_SECRET: 应用 Secret（必填，未设置则返回 None）
    - ADMIN_FEISHU_RECEIVE_ID: 接收者 ID（必填，未设置则返回 None）
    - ADMIN_FEISHU_RECEIVE_ID_TYPE: 接收者类型（可选，默认 user_id）

    Returns:
        {"app_id": str, "app_secret": str, "receive_id": str, "receive_id_type": str}
        或 None（必填项未配置）
    """
    app_id = os.environ.get("ADMIN_FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("ADMIN_FEISHU_APP_SECRET", "").strip()
    receive_id = os.environ.get("ADMIN_FEISHU_RECEIVE_ID", "").strip()
    receive_id_type = os.environ.get("ADMIN_FEISHU_RECEIVE_ID_TYPE", "").strip() or "user_id"

    # 三项必填，任一缺失返回 None
    if not app_id or not app_secret or not receive_id:
        return None

    return {
        "app_id": app_id,
        "app_secret": app_secret,
        "receive_id": receive_id,
        "receive_id_type": receive_id_type,
    }


if __name__ == "__main__":
    # 自测入口：验证配置读取逻辑（无副作用，不连接网络）

    # 场景 1：未配置返回 None
    for key in (
        "ADMIN_FEISHU_APP_ID",
        "ADMIN_FEISHU_APP_SECRET",
        "ADMIN_FEISHU_RECEIVE_ID",
        "ADMIN_FEISHU_RECEIVE_ID_TYPE",
    ):
        os.environ.pop(key, None)
    assert get_admin_feishu_config() is None, "未配置应返回 None"
    print("scenario_1_none: OK")

    # 场景 2：完整 Platform App 配置（receive_id_type 显式设置）
    os.environ["ADMIN_FEISHU_APP_ID"] = "cli_test_app_001"
    os.environ["ADMIN_FEISHU_APP_SECRET"] = "test_secret_value"
    os.environ["ADMIN_FEISHU_RECEIVE_ID"] = "bg33237"
    os.environ["ADMIN_FEISHU_RECEIVE_ID_TYPE"] = "user_id"
    config = get_admin_feishu_config()
    assert config is not None
    assert config["app_id"] == "cli_test_app_001"
    assert config["app_secret"] == "test_secret_value"
    assert config["receive_id"] == "bg33237"
    assert config["receive_id_type"] == "user_id"
    # 不应包含 webhook 字段
    assert "webhook_url" not in config
    assert "sign_secret" not in config
    print(f"scenario_2_full_config: {config}")

    # 场景 3：receive_id_type 未设置时默认 user_id
    os.environ.pop("ADMIN_FEISHU_RECEIVE_ID_TYPE", None)
    config = get_admin_feishu_config()
    assert config is not None
    assert config["receive_id_type"] == "user_id"
    print("scenario_3_default_type: OK")

    # 场景 4：必填项缺失返回 None（仅 app_id 设置）
    os.environ.pop("ADMIN_FEISHU_APP_SECRET", None)
    config = get_admin_feishu_config()
    assert config is None, "app_secret 缺失应返回 None"
    print("scenario_4_missing_secret: OK")

    # 场景 5：空白字符被 strip
    os.environ["ADMIN_FEISHU_APP_ID"] = "  cli_test_app_002  "
    os.environ["ADMIN_FEISHU_APP_SECRET"] = "  secret  "
    os.environ["ADMIN_FEISHU_RECEIVE_ID"] = "  bg12345  "
    config = get_admin_feishu_config()
    assert config is not None
    assert config["app_id"] == "cli_test_app_002"
    assert config["app_secret"] == "secret"
    assert config["receive_id"] == "bg12345"
    print("scenario_5_strip: OK")

    # 清理
    for key in (
        "ADMIN_FEISHU_APP_ID",
        "ADMIN_FEISHU_APP_SECRET",
        "ADMIN_FEISHU_RECEIVE_ID",
        "ADMIN_FEISHU_RECEIVE_ID_TYPE",
    ):
        os.environ.pop(key, None)
    print("OK")
