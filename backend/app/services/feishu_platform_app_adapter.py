"""飞书平台应用 Channel Adapter - App API 模式投递。

与 Webhook 模式区别：
- 通过 app_id + app_secret 获取 tenant_access_token
- 通过 im/v1/messages API 向指定 receive_id 发送消息
- 支持向个人用户发送（非群机器人）
- content 为卡片 JSON 字符串（与 Webhook 的 card 字段结构一致）

channel_config 结构（用户级）：
    {
        "app_id": "cli_xxxxx",          # 飞书应用 ID
        "app_secret": "xxxxx",           # 飞书应用 Secret
        "receive_id": "bg33237",         # 飞书接收者 ID
        "receive_id_type": "user_id"     # user_id/open_id/union_id
    }

How to Run:
    python -m app.services.feishu_platform_app_adapter    # 自测：发送测试消息
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, ClassVar

import httpx

from app.schemas.notification import DeliveryResult, NotificationMessageDTO
from app.services.channel_adapter import ChannelAdapter, register_adapter
from app.services.feishu_card_builder import dto_to_feishu_card

logger = logging.getLogger("feishu_platform_app_adapter")

# 飞书开放平台 API 地址
_TOKEN_API_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_MESSAGES_API_URL = "https://open.feishu.cn/open-apis/im/v1/messages"

# HTTP 超时（秒）
_HTTP_TIMEOUT = 10.0

# 可重试的 HTTP 状态码（429 限流 / 5xx 服务端错误）
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# 渠道失效的 HTTP 状态码（4xx 配置错误）
_INVALID_STATUS = {400, 401, 403, 404}

# Token 缓存：key=app_id, value=(tenant_access_token, expire_timestamp)
_token_cache: dict[str, tuple[str, float]] = {}

# Token 提前刷新时间（秒）
_TOKEN_REFRESH_MARGIN = 300.0


async def _get_tenant_access_token(app_id: str, app_secret: str) -> tuple[str, str | None]:
    """获取飞书 tenant_access_token（带缓存）。

    缓存策略：token 提前 300 秒刷新，避免临界过期。

    Args:
        app_id: 飞书应用 ID
        app_secret: 飞书应用 Secret

    Returns:
        (tenant_access_token, error_message)
        - 成功: (token, None)
        - 失败: ("", error_message)
    """
    now = time.time()
    cached = _token_cache.get(app_id)
    if cached:
        token, expire_at = cached
        if now < expire_at - _TOKEN_REFRESH_MARGIN:
            return token, None

    # 请求新 token
    payload = {"app_id": app_id, "app_secret": app_secret}
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(_TOKEN_API_URL, json=payload)
    except httpx.HTTPError as e:
        error_msg = f"获取 tenant_access_token 网络错误: {e}"
        logger.error(error_msg)
        return "", error_msg

    if resp.status_code != 200:
        error_msg = f"获取 tenant_access_token HTTP 错误: {resp.status_code} {resp.text[:200]}"
        logger.error(error_msg)
        return "", error_msg

    try:
        result = resp.json()
    except Exception as e:
        error_msg = f"获取 tenant_access_token 响应解析失败: {e}"
        logger.error(error_msg)
        return "", error_msg

    if result.get("code") != 0:
        error_msg = f"获取 tenant_access_token 失败: code={result.get('code')} msg={result.get('msg')}"
        logger.error(error_msg)
        return "", error_msg

    token = result.get("tenant_access_token", "")
    expire_seconds = result.get("expire", 7200)
    expire_at = now + expire_seconds

    _token_cache[app_id] = (token, expire_at)
    logger.info("获取 tenant_access_token 成功: app_id=%s expire_in=%ds", app_id, expire_seconds)
    return token, None


@register_adapter
class FeishuPlatformAppAdapter(ChannelAdapter):
    """飞书平台应用渠道适配器 - App API 模式。

    通过 app_id + app_secret 获取 tenant_access_token，
    调用 im/v1/messages API 向指定 receive_id 发送消息。

    凭证来源：
    - app_id/app_secret/receive_id/receive_id_type: 均从 channel_config 用户级配置读取
    """

    adapter_type: ClassVar[str] = "feishu_platform_app"

    async def send(
        self,
        message_dto: NotificationMessageDTO,
        channel_config: dict[str, Any],
    ) -> DeliveryResult:
        """投递消息到飞书平台应用。

        Args:
            message_dto: 统一消息 DTO
            channel_config: 用户级渠道配置（含 app_id/app_secret/receive_id/receive_id_type）

        Returns:
            DeliveryResult（success=True 表示飞书返回 code=0）
            - 429/5xx: success=False, error_code=RETRYABLE
            - 4xx: success=False, error_code=CHANNEL_INVALID
            - 网络异常: success=False, error_code=NETWORK_ERROR
            - 配置缺失: success=False, error_code=CONFIG_MISSING
            - Token 获取失败: success=False, error_code=AUTH_FAILED
        """
        # 从 channel_config 读取用户级凭证
        app_id = channel_config.get("app_id", "")
        app_secret = channel_config.get("app_secret", "")
        receive_id = channel_config.get("receive_id", "")
        receive_id_type = channel_config.get("receive_id_type", "user_id")

        if not app_id or not app_secret:
            return DeliveryResult(
                success=False,
                error_code="CONFIG_MISSING",
                error_message="channel_config 缺少: app_id 或 app_secret",
            )

        if not receive_id:
            return DeliveryResult(
                success=False,
                error_code="CONFIG_MISSING",
                error_message="channel_config 缺少: receive_id",
            )

        # 获取 tenant_access_token
        token, token_error = await _get_tenant_access_token(app_id, app_secret)
        if token_error:
            return DeliveryResult(
                success=False,
                error_code="AUTH_FAILED",
                error_message=token_error,
            )

        # 构建消息体
        card = dto_to_feishu_card(message_dto)
        content = json.dumps(card)
        payload = {
            "receive_id": receive_id,
            "msg_type": "interactive",
            "content": content,
        }
        headers = {"Authorization": f"Bearer {token}"}
        params = {"receive_id_type": receive_id_type}

        # 发送消息
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(
                    _MESSAGES_API_URL,
                    json=payload,
                    headers=headers,
                    params=params,
                )
        except httpx.TimeoutException as e:
            logger.warning(
                "飞书平台应用超时: title=%s receive_id=%s: %s",
                message_dto.title, receive_id, e,
            )
            return DeliveryResult(
                success=False,
                error_code="NETWORK_TIMEOUT",
                error_message=f"请求超时: {e}",
            )
        except httpx.HTTPError as e:
            logger.warning(
                "飞书平台应用网络错误: title=%s receive_id=%s: %s",
                message_dto.title, receive_id, e,
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
                "飞书平台应用投递成功: title=%s receive_id=%s",
                message_dto.title, receive_id,
            )
            return DeliveryResult(
                success=True,
                provider_response=result,
            )

        # 飞书返回错误码
        error_code = str(result.get("code", "UNKNOWN"))
        error_msg = result.get("msg", "未知错误")
        logger.warning(
            "飞书平台应用投递失败: title=%s code=%s msg=%s",
            message_dto.title, error_code, error_msg,
        )
        return DeliveryResult(
            success=False,
            error_code=f"FEISHU_{error_code}",
            error_message=error_msg,
            provider_response=result,
        )

    async def send_image_bytes(
        self,
        image_bytes: bytes,
        channel_config: dict[str, Any],
    ) -> DeliveryResult:
        """发送图片 bytes 到飞书平台应用。

        流程：
        1. 获取 tenant_access_token
        2. 上传图片 bytes 到飞书获取 image_key
        3. 发送图片消息

        Args:
            image_bytes: PNG 图片 bytes
            channel_config: 用户级渠道配置（含 app_id/app_secret/receive_id/receive_id_type）

        Returns:
            DeliveryResult（success=True 表示飞书返回 code=0）
        """
        if not image_bytes or len(image_bytes) < 100:
            return DeliveryResult(
                success=False,
                error_code="INVALID_IMAGE",
                error_message="图片 bytes 为空或过小",
            )

        # 从 channel_config 读取用户级凭证
        app_id = channel_config.get("app_id", "")
        app_secret = channel_config.get("app_secret", "")
        receive_id = channel_config.get("receive_id", "")
        receive_id_type = channel_config.get("receive_id_type", "user_id")

        if not app_id or not app_secret:
            return DeliveryResult(
                success=False,
                error_code="CONFIG_MISSING",
                error_message="channel_config 缺少: app_id 或 app_secret",
            )

        if not receive_id:
            return DeliveryResult(
                success=False,
                error_code="CONFIG_MISSING",
                error_message="channel_config 缺少: receive_id",
            )

        # 获取 tenant_access_token
        token, token_error = await _get_tenant_access_token(app_id, app_secret)
        if token_error:
            return DeliveryResult(
                success=False,
                error_code="AUTH_FAILED",
                error_message=token_error,
            )

        headers = {"Authorization": f"Bearer {token}"}

        # 上传图片 bytes
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                upload_resp = await client.post(
                    "https://open.feishu.cn/open-apis/im/v1/images",
                    headers=headers,
                    data={"image_type": "message"},
                    files={"image": ("image.png", image_bytes, "image/png")},
                )
        except httpx.TimeoutException as e:
            logger.warning("飞书图片上传超时: %s", e)
            return DeliveryResult(
                success=False,
                error_code="NETWORK_TIMEOUT",
                error_message=f"图片上传超时: {e}",
            )
        except httpx.HTTPError as e:
            logger.warning("飞书图片上传网络错误: %s", e)
            return DeliveryResult(
                success=False,
                error_code="NETWORK_ERROR",
                error_message=f"图片上传网络错误: {e}",
            )

        # 检查上传响应
        if upload_resp.status_code in _RETRYABLE_STATUS:
            return DeliveryResult(
                success=False,
                error_code="RETRYABLE",
                error_message=f"图片上传 HTTP {upload_resp.status_code}: {upload_resp.text[:200]}",
            )

        if upload_resp.status_code in _INVALID_STATUS:
            return DeliveryResult(
                success=False,
                error_code="CHANNEL_INVALID",
                error_message=f"图片上传 HTTP {upload_resp.status_code}: 渠道配置无效",
            )

        if upload_resp.status_code != 200:
            return DeliveryResult(
                success=False,
                error_code=f"HTTP_{upload_resp.status_code}",
                error_message=f"图片上传 HTTP {upload_resp.status_code}: {upload_resp.text[:200]}",
            )

        try:
            upload_result = upload_resp.json()
        except Exception as e:
            return DeliveryResult(
                success=False,
                error_code="RESPONSE_PARSE_ERROR",
                error_message=f"图片上传响应解析失败: {e}",
            )

        if upload_result.get("code") != 0:
            error_code = str(upload_result.get("code", "UNKNOWN"))
            error_msg = upload_result.get("msg", "未知错误")
            logger.warning("飞书图片上传失败: code=%s msg=%s", error_code, error_msg)
            return DeliveryResult(
                success=False,
                error_code=f"FEISHU_{error_code}",
                error_message=error_msg,
            )

        image_key = upload_result.get("data", {}).get("image_key", "")
        if not image_key:
            return DeliveryResult(
                success=False,
                error_code="IMAGE_KEY_MISSING",
                error_message="飞书图片上传成功但未返回 image_key",
            )

        # 发送图片消息
        content = json.dumps({"image_key": image_key})
        payload = {
            "receive_id": receive_id,
            "msg_type": "image",
            "content": content,
        }
        params = {"receive_id_type": receive_id_type}

        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(
                    _MESSAGES_API_URL,
                    json=payload,
                    headers=headers,
                    params=params,
                )
        except httpx.TimeoutException as e:
            logger.warning("飞书图片消息发送超时: receive_id=%s: %s", receive_id, e)
            return DeliveryResult(
                success=False,
                error_code="NETWORK_TIMEOUT",
                error_message=f"图片消息发送超时: {e}",
            )
        except httpx.HTTPError as e:
            logger.warning("飞书图片消息发送网络错误: receive_id=%s: %s", receive_id, e)
            return DeliveryResult(
                success=False,
                error_code="NETWORK_ERROR",
                error_message=f"图片消息发送网络错误: {e}",
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

        if result.get("code") == 0:
            logger.info(
                "飞书图片消息投递成功: receive_id=%s image_key=%s",
                receive_id, image_key,
            )
            return DeliveryResult(
                success=True,
                provider_response=result,
            )

        error_code = str(result.get("code", "UNKNOWN"))
        error_msg = result.get("msg", "未知错误")
        logger.warning(
            "飞书图片消息投递失败: receive_id=%s code=%s msg=%s",
            receive_id, error_code, error_msg,
        )
        return DeliveryResult(
            success=False,
            error_code=f"FEISHU_{error_code}",
            error_message=error_msg,
            provider_response=result,
        )

    async def send_text_message(
        self,
        message_dto: NotificationMessageDTO,
        channel_config: dict[str, Any],
    ) -> DeliveryResult:
        """发送纯文本消息到飞书平台应用。

        [飞书两段式投递] - delivery_type=text 时调用。
        从 message_dto.text_content 读取纯文本内容，通过飞书 im/v1/messages API
        以 msg_type=text 发送到指定 receive_id。

        Args:
            message_dto: 统一消息 DTO（text_content 字段含纯文本内容）
            channel_config: 用户级渠道配置（含 app_id/app_secret/receive_id/receive_id_type）

        Returns:
            DeliveryResult（success=True 表示飞书返回 code=0）
        """
        # 从 channel_config 读取用户级凭证
        app_id = channel_config.get("app_id", "")
        app_secret = channel_config.get("app_secret", "")
        receive_id = channel_config.get("receive_id", "")
        receive_id_type = channel_config.get("receive_id_type", "user_id")

        if not app_id or not app_secret:
            return DeliveryResult(
                success=False,
                error_code="CONFIG_MISSING",
                error_message="channel_config 缺少: app_id 或 app_secret",
            )

        if not receive_id:
            return DeliveryResult(
                success=False,
                error_code="CONFIG_MISSING",
                error_message="channel_config 缺少: receive_id",
            )

        # 纯文本内容：优先 text_content，回退到 summary
        text_content = message_dto.text_content or message_dto.summary
        if not text_content:
            return DeliveryResult(
                success=False,
                error_code="TEXT_CONTENT_MISSING",
                error_message="message_dto.text_content 与 summary 均为空",
            )

        # 获取 tenant_access_token
        token, token_error = await _get_tenant_access_token(app_id, app_secret)
        if token_error:
            return DeliveryResult(
                success=False,
                error_code="AUTH_FAILED",
                error_message=token_error,
            )

        # 构建文本消息 payload
        content = json.dumps({"text": text_content})
        payload = {
            "receive_id": receive_id,
            "msg_type": "text",
            "content": content,
        }
        headers = {"Authorization": f"Bearer {token}"}
        params = {"receive_id_type": receive_id_type}

        # 发送消息
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.post(
                    _MESSAGES_API_URL,
                    json=payload,
                    headers=headers,
                    params=params,
                )
        except httpx.TimeoutException as e:
            logger.warning(
                "飞书平台应用文本消息超时: receive_id=%s: %s",
                receive_id, e,
            )
            return DeliveryResult(
                success=False,
                error_code="NETWORK_TIMEOUT",
                error_message=f"请求超时: {e}",
            )
        except httpx.HTTPError as e:
            logger.warning(
                "飞书平台应用文本消息网络错误: receive_id=%s: %s",
                receive_id, e,
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
                "飞书平台应用文本消息投递成功: receive_id=%s",
                receive_id,
            )
            return DeliveryResult(
                success=True,
                provider_response=result,
            )

        # 飞书返回错误码
        error_code = str(result.get("code", "UNKNOWN"))
        error_msg = result.get("msg", "未知错误")
        logger.warning(
            "飞书平台应用文本消息投递失败: receive_id=%s code=%s msg=%s",
            receive_id, error_code, error_msg,
        )
        return DeliveryResult(
            success=False,
            error_code=f"FEISHU_{error_code}",
            error_message=error_msg,
            provider_response=result,
        )

    async def verify(self, channel_config: dict[str, Any]) -> bool:
        """验证飞书平台应用配置有效性。

        发送一条测试消息到指定用户，确认凭证与 receive_id 配置可用。

        Args:
            channel_config: 用户级渠道配置（含 app_id/app_secret/receive_id/receive_id_type）

        Returns:
            True（验证成功），False（验证失败）
        """
        receive_id = channel_config.get("receive_id", "")

        if not receive_id:
            return False

        # 构建测试消息
        test_dto = NotificationMessageDTO(
            message_type="SYSTEM_ALERT",
            template_key="system_alert",
            template_version="1.1.0",
            title="渠道验证测试",
            summary="这是一条渠道验证测试消息，确认飞书平台应用配置有效。",
            resource_refs={"test": True},
            data_time=str(int(time.time())),
        )

        result = await self.send(test_dto, channel_config)
        if result.success:
            logger.info("飞书平台应用验证成功: receive_id=%s", receive_id)
        else:
            logger.warning(
                "飞书平台应用验证失败: receive_id=%s error=%s",
                receive_id, result.error_message,
            )
        return result.success


if __name__ == "__main__":
    # 自测入口：从 DB 查询用户配置的渠道凭证
    import asyncio

    from app.db import AsyncSessionLocal
    from app.models.notification import NotificationChannel
    from app.services.channel_adapter import get_adapter, list_supported_adapters
    from sqlalchemy import select

    async def _test():
        # 验证注册
        print(f"已注册适配器: {list_supported_adapters()}")
        assert "feishu_platform_app" in list_supported_adapters()

        # 验证 adapter 实例化
        adapter = get_adapter("feishu_platform_app")
        print(f"adapter_type={adapter.adapter_type}")
        assert adapter.adapter_type == "feishu_platform_app"

        # 从 DB 查询第一个活跃的平台应用渠道
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(NotificationChannel)
                .where(
                    NotificationChannel.adapter_type == "feishu_platform_app",
                    NotificationChannel.status == "active",
                )
                .limit(1)
            )
            channel = result.scalar_one_or_none()

        if channel is None:
            print("跳过发送测试: DB 中无活跃的飞书平台应用渠道配置")
            print("请先在前端设置页配置飞书应用通知渠道")
            return

        channel_config = channel.target_config
        print(f"使用渠道: {channel.display_name} (id={channel.id})")

        # 构建测试 DTO
        test_dto = NotificationMessageDTO(
            message_type="SYSTEM_ALERT",
            template_key="system_alert",
            template_version="1.1.0",
            title="平台应用测试消息",
            summary="这是一条飞书平台应用适配器的测试消息。",
            resource_refs={"test": True},
            data_time=str(int(time.time())),
        )

        result = await adapter.send(test_dto, channel_config)
        print(f"send result: success={result.success}")
        print(f"error_code={result.error_code}")
        print(f"error_message={result.error_message}")
        if result.provider_response:
            print(f"provider_response={result.provider_response}")

        print("OK")

    asyncio.run(_test())
