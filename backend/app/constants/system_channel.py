"""系统级飞书渠道配置 - 从环境变量读取管理员飞书 Webhook。

设计（spec 第四节）：
- 系统级渠道不入库，不暴露前端
- 生产配置中只能有一个 active 管理员飞书目标
- 未配置时返回 None，调用方负责处理（标记 failed 但不影响用户提交）
- 普通用户的飞书渠道（NotificationChannel.user_id NOT NULL）不适用于管理员通知

环境变量：
- ADMIN_FEISHU_WEBHOOK_URL: 管理员飞书 Webhook URL（必填）
- ADMIN_FEISHU_SIGN_SECRET: 签名密钥（可选，飞书 Webhook 签名校验）

用法:
    from app.constants.system_channel import get_admin_feishu_config

    config = get_admin_feishu_config()
    if config is None:
        # 未配置，标记 failed 但不影响用户提交
        return
    # config = {"webhook_url": ..., "sign_secret": ...}
    adapter.send(dto, config)
"""

from __future__ import annotations

import os
from typing import Any


def get_admin_feishu_config() -> dict[str, Any] | None:
    """返回管理员飞书 Webhook 配置。

    从环境变量读取：
    - ADMIN_FEISHU_WEBHOOK_URL: Webhook URL（必填，未设置则返回 None）
    - ADMIN_FEISHU_SIGN_SECRET: 签名密钥（可选）

    Returns:
        {"webhook_url": str, "sign_secret": str} 或 None（未配置）
        sign_secret 仅在设置时包含在返回 dict 中
    """
    webhook_url = os.environ.get("ADMIN_FEISHU_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return None

    sign_secret = os.environ.get("ADMIN_FEISHU_SIGN_SECRET", "").strip()
    config: dict[str, Any] = {"webhook_url": webhook_url}
    if sign_secret:
        config["sign_secret"] = sign_secret
    return config


if __name__ == "__main__":
    # 自测入口：验证配置读取逻辑（无副作用，不连接网络）

    # 场景 1：未配置返回 None
    os.environ.pop("ADMIN_FEISHU_WEBHOOK_URL", None)
    os.environ.pop("ADMIN_FEISHU_SIGN_SECRET", None)
    assert get_admin_feishu_config() is None, "未配置应返回 None"
    print("scenario_1_none: OK")

    # 场景 2：仅 webhook_url
    os.environ["ADMIN_FEISHU_WEBHOOK_URL"] = "https://open.feishu.cn/open-apis/bot/v2/hook/test"
    config = get_admin_feishu_config()
    assert config is not None
    assert config["webhook_url"] == "https://open.feishu.cn/open-apis/bot/v2/hook/test"
    assert "sign_secret" not in config
    print(f"scenario_2_webhook_only: {config}")

    # 场景 3：webhook_url + sign_secret
    os.environ["ADMIN_FEISHU_SIGN_SECRET"] = "secret123"
    config = get_admin_feishu_config()
    assert config is not None
    assert config["webhook_url"] == "https://open.feishu.cn/open-apis/bot/v2/hook/test"
    assert config["sign_secret"] == "secret123"
    print(f"scenario_3_with_secret: {config}")

    # 场景 4：空白字符被 strip
    os.environ["ADMIN_FEISHU_WEBHOOK_URL"] = "  https://open.feishu.cn/open-apis/bot/v2/hook/test  "
    config = get_admin_feishu_config()
    assert config is not None
    assert config["webhook_url"] == "https://open.feishu.cn/open-apis/bot/v2/hook/test"
    print("scenario_4_strip: OK")

    # 清理
    os.environ.pop("ADMIN_FEISHU_WEBHOOK_URL", None)
    os.environ.pop("ADMIN_FEISHU_SIGN_SECRET", None)
    print("OK")
