"""飞书 Webhook Channel Adapter - Webhook + 签名加密投递。

重构自 ref/交易/app/feishu_notifier.py，改为：
- Webhook 模式（非平台应用模式），使用 HMAC-SHA256 签名
- 异步 httpx（替代同步 requests）
- 只负责格式转换+发送（ChannelAdapter 职责）
- 429/5xx 可重试，4xx 标记渠道失效
- 卡片格式由 feishu_card_builder 共享（预览与投递一致）

channel_config 结构：
    {
        "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/xxxxx",
        "sign_secret": "签名密钥"
    }

签名算法（飞书 Webhook 签名校验）：
    timestamp = str(int(time.time()))
    string_to_sign = timestamp + "\\n" + secret
    sign = base64(hmac_sha256(string_to_sign))

How to Run:
    python -m app.services.feishu_webhook_adapter    # 自测：验证签名与卡片构建
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from typing import Any, ClassVar

import httpx

from app.schemas.notification import DeliveryResult, NotificationMessageDTO
from app.services.channel_adapter import ChannelAdapter, register_adapter
from app.services.feishu_card_builder import dto_to_feishu_card

logger = logging.getLogger("feishu_webhook_adapter")

# 飞书 Webhook API 地址前缀（校验 URL 合法性）
_FEISHU_WEBHOOK_PREFIX = "https://open.feishu.cn/open-apis/bot/v2/hook/"

# HTTP 超时（秒）
_HTTP_TIMEOUT = 10.0

# 可重试的 HTTP 状态码（429 限流 / 5xx 服务端错误）
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# 渠道失效的 HTTP 状态码（4xx 配置错误）
_INVALID_STATUS = {400, 401, 403, 404}


def _sign(timestamp: str, secret: str) -> str:
    """计算飞书 Webhook 签名。

    算法：
        string_to_sign = timestamp + "\\n" + secret
        sign = base64(hmac_sha256(string_to_sign))

    Args:
        timestamp: Unix 时间戳字符串
        secret: 签名密钥

    Returns:
        Base64 编码的签名字符串
    """
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def _build_webhook_payload(
    dto: NotificationMessageDTO,
    sign_secret: str | None,
) -> dict[str, Any]:
    """构建飞书 Webhook 请求体。

    Args:
        dto: 统一消息 DTO
        sign_secret: 签名密钥（为空则不签名）

    Returns:
        飞书 Webhook 请求体 JSON
    """
    card = dto_to_feishu_card(dto)
    payload: dict[str, Any] = {
        "msg_type": "interactive",
        "card": card,
    }
    if sign_secret:
        timestamp = str(int(time.time()))
        payload["timestamp"] = timestamp
        payload["sign"] = _sign(timestamp, sign_secret)
    return payload


@register_adapter
class FeishuWebhookAdapter(ChannelAdapter):
    """飞书 Webhook 渠道适配器。

    只负责格式转换（DTO → 飞书卡片）+ 签名 + 发送到 Webhook URL。
    不新增业务字段，不处理重试策略（由调用方决定）。
    """

    adapter_type: ClassVar[str] = "feishu_webhook"

    async def send(
        self,
        message_dto: NotificationMessageDTO,
        channel_config: dict[str, Any],
    ) -> DeliveryResult:
        """投递消息到飞书 Webhook。

        Args:
            message_dto: 统一消息 DTO
            channel_config: 渠道配置（含 webhook_url 和 sign_secret）

        Returns:
            DeliveryResult（success=True 表示飞书返回 code=0）
            - 429/5xx: success=False, error_code=RETRYABLE
            - 4xx: success=False, error_code=CHANNEL_INVALID
            - 网络异常: success=False, error_code=NETWORK_ERROR
        """
        webhook_url = channel_config.get("webhook_url", "")
        sign_secret = channel_config.get("sign_secret")

        if not webhook_url:
            return DeliveryResult(
                success=False,
                error_code="CONFIG_MISSING",
                error_message="channel_config 缺少 webhook_url",
            )

        payload = _build_webhook_payload(message_dto, sign_secret)

        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(webhook_url, json=payload)
        except httpx.TimeoutException as e:
            logger.warning(
                "飞书 Webhook 超时: title=%s url=%s: %s",
                message_dto.title, webhook_url[:60], e,
            )
            return DeliveryResult(
                success=False,
                error_code="NETWORK_TIMEOUT",
                error_message=f"请求超时: {e}",
            )
        except httpx.HTTPError as e:
            logger.warning(
                "飞书 Webhook 网络错误: title=%s url=%s: %s",
                message_dto.title, webhook_url[:60], e,
            )
            return DeliveryResult(
                success=False,
                error_code="NETWORK_ERROR",
                error_message=f"网络错误: {e}",
            )

        # 检查 HTTP 状态码
        if resp.status_code in _RETRYABLE_STATUS:
            return DeliveryResult(
                success=False,
                error_code="RETRYABLE",
                error_message=f"HTTP {resp.status_code}: {resp.text[:200]}",
                provider_response={"status_code": resp.status_code, "body": resp.text[:500]},
            )

        if resp.status_code in _INVALID_STATUS:
            return DeliveryResult(
                success=False,
                error_code="CHANNEL_INVALID",
                error_message=f"HTTP {resp.status_code}: 渠道配置无效",
                provider_response={"status_code": resp.status_code, "body": resp.text[:500]},
            )

        if resp.status_code != 200:
            return DeliveryResult(
                success=False,
                error_code=f"HTTP_{resp.status_code}",
                error_message=f"HTTP {resp.status_code}: {resp.text[:200]}",
                provider_response={"status_code": resp.status_code, "body": resp.text[:500]},
            )

        # 解析飞书响应
        try:
            result = resp.json()
        except Exception as e:
            return DeliveryResult(
                success=False,
                error_code="RESPONSE_PARSE_ERROR",
                error_message=f"响应解析失败: {e}",
                provider_response={"raw": resp.text[:500]},
            )

        # 飞书返回 code=0 表示成功
        if result.get("code") == 0:
            logger.info(
                "飞书 Webhook 投递成功: title=%s StatusCode=%s",
                message_dto.title, resp.status_code,
            )
            return DeliveryResult(
                success=True,
                provider_response=result,
            )

        # 飞书返回错误码
        error_code = str(result.get("code", "UNKNOWN"))
        error_msg = result.get("msg", "未知错误")
        logger.warning(
            "飞书 Webhook 投递失败: title=%s code=%s msg=%s",
            message_dto.title, error_code, error_msg,
        )
        return DeliveryResult(
            success=False,
            error_code=f"FEISHU_{error_code}",
            error_message=error_msg,
            provider_response=result,
        )

    async def verify(self, channel_config: dict[str, Any]) -> bool:
        """验证飞书 Webhook 配置有效性。

        发送一条测试消息到 Webhook，确认 URL 和签名配置可用。

        Args:
            channel_config: 渠道配置（含 webhook_url 和 sign_secret）

        Returns:
            True（验证成功），False（验证失败）
        """
        webhook_url = channel_config.get("webhook_url", "")
        sign_secret = channel_config.get("sign_secret")

        if not webhook_url:
            return False

        # 构建测试消息
        test_dto = NotificationMessageDTO(
            message_type="SYSTEM_ALERT",
            template_key="system_alert",
            template_version="1.1.0",
            title="渠道验证测试",
            summary="这是一条渠道验证测试消息，确认 Webhook 配置有效。",
            resource_refs={"test": True},
            data_time=str(int(time.time())),
        )

        payload = _build_webhook_payload(test_dto, sign_secret)

        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(webhook_url, json=payload)
        except httpx.HTTPError as e:
            logger.warning(
                "飞书 Webhook 验证失败（网络错误）: url=%s: %s",
                webhook_url[:60], e,
            )
            return False

        if resp.status_code != 200:
            logger.warning(
                "飞书 Webhook 验证失败（HTTP %s）: url=%s",
                resp.status_code, webhook_url[:60],
            )
            return False

        try:
            result = resp.json()
        except Exception:
            return False

        verified = result.get("code") == 0
        if verified:
            logger.info("飞书 Webhook 验证成功: url=%s", webhook_url[:60])
        else:
            logger.warning(
                "飞书 Webhook 验证失败（code=%s msg=%s）: url=%s",
                result.get("code"), result.get("msg"), webhook_url[:60],
            )
        return verified


if __name__ == "__main__":
    # 自测入口：验证签名与卡片构建（不发送网络请求）
    from app.services.channel_adapter import get_adapter, list_supported_adapters

    # 验证注册
    print(f"已注册适配器: {list_supported_adapters()}")
    assert "feishu_webhook" in list_supported_adapters()

    # 验证签名
    ts = "1597362936"
    secret = "test_secret"
    sign = _sign(ts, secret)
    print(f"sign={sign[:20]}...")
    assert len(sign) > 0

    # 验证 payload 构建
    dto = NotificationMessageDTO(
        message_type="SYSTEM_ALERT",
        template_key="system_alert",
        template_version="1.1.0",
        title="测试消息",
        summary="测试摘要",
        resource_refs={"test": True},
        data_time="2026-06-18T10:00:00+08:00",
    )
    payload = _build_webhook_payload(dto, secret)
    print(f"msg_type={payload['msg_type']}")
    print(f"has_timestamp={'timestamp' in payload}")
    print(f"has_sign={'sign' in payload}")
    print(f"card_header={payload['card']['header']['title']['content']}")
    assert payload["msg_type"] == "interactive"
    assert "timestamp" in payload
    assert "sign" in payload
    assert payload["card"]["header"]["title"]["content"] == "测试消息"

    # 验证无签名时 payload 不含 sign
    payload_no_sign = _build_webhook_payload(dto, None)
    assert "timestamp" not in payload_no_sign
    assert "sign" not in payload_no_sign

    # 验证 adapter 实例化
    adapter = get_adapter("feishu_webhook")
    print(f"adapter_type={adapter.adapter_type}")
    assert adapter.adapter_type == "feishu_webhook"

    print("OK")
