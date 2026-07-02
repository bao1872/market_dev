"""渠道适配器抽象 - ChannelAdapter ABC。

设计：
- ChannelAdapter: 抽象基类，定义 send/verify 接口
- send(message_dto, channel_config) -> DeliveryResult: 投递消息
- verify(channel_config) -> bool: 验证渠道配置有效性

已注册实现：
- MockChannelAdapter: 测试/开发环境（不实际发送）
- FeishuWebhookAdapter: 飞书 Webhook 渠道（签名加密）
- FeishuPlatformAppAdapter: 飞书平台应用渠道（App API 模式）

后续实现：
- EmailAdapter: 邮件渠道
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from app.schemas.notification import DeliveryResult, NotificationMessageDTO


class ChannelAdapter(ABC):
    """渠道适配器抽象基类。

    每种通知渠道（飞书 webhook/平台应用/邮件）实现一个子类。
    Adapter 不新增业务字段，仅按渠道格式化与投递。

    子类必须定义类属性 adapter_type（ClassVar[str]），用于注册与查找。
    """

    adapter_type: ClassVar[str]

    @abstractmethod
    async def send(
        self,
        message_dto: NotificationMessageDTO,
        channel_config: dict[str, Any],
    ) -> DeliveryResult:
        """投递消息到渠道。

        Args:
            message_dto: 统一消息 DTO
            channel_config: 渠道配置（如 webhook URL、签名 secret 引用等）

        Returns:
            DeliveryResult 投递结果

        说明：
            - 失败时返回 success=False 的 DeliveryResult，不抛异常
            - 429/5xx/网络错误由调用方决定重试策略
            - 配置型 4xx 标记渠道失效
        """

    @abstractmethod
    async def verify(self, channel_config: dict[str, Any]) -> bool:
        """验证渠道配置有效性。

        Args:
            channel_config: 渠道配置

        Returns:
            True（验证成功），False（验证失败）

        说明：
            - 发送验证消息到渠道，确认配置可用
            - 验证成功后渠道状态变为 active
        """

    async def send_image_bytes(
        self,
        image_bytes: bytes,
        channel_config: dict[str, Any],
    ) -> DeliveryResult:
        """发送图片 bytes 到渠道（可选实现）。

        默认返回 NOT_SUPPORTED；支持图片投递的渠道（如飞书平台应用）应覆盖此方法。

        Args:
            image_bytes: PNG 图片 bytes
            channel_config: 渠道配置

        Returns:
            DeliveryResult
        """
        return DeliveryResult(
            success=False,
            error_code="NOT_SUPPORTED",
            error_message=f"渠道类型 {self.adapter_type} 不支持图片投递",
            image_upload_success=False,
            image_upload_error_code="NOT_SUPPORTED",
            image_upload_error_message=f"渠道类型 {self.adapter_type} 不支持图片投递",
        )

    async def send_text_message(
        self,
        message_dto: NotificationMessageDTO,
        channel_config: dict[str, Any],
    ) -> DeliveryResult:
        """发送纯文本消息到渠道（可选实现）。

        [飞书两段式投递] - delivery_type=text 时调用。
        默认返回 NOT_SUPPORTED；支持文本投递的渠道（如飞书平台应用）应覆盖此方法。

        Args:
            message_dto: 统一消息 DTO（text_content 字段含纯文本内容）
            channel_config: 渠道配置

        Returns:
            DeliveryResult
        """
        return DeliveryResult(
            success=False,
            error_code="NOT_SUPPORTED",
            error_message=f"渠道类型 {self.adapter_type} 不支持纯文本投递",
        )


# 渠道适配器注册表
_ADAPTER_REGISTRY: dict[str, type[ChannelAdapter]] = {}


def register_adapter(adapter_cls: type[ChannelAdapter]) -> type[ChannelAdapter]:
    """注册渠道适配器（装饰器用法）。

    用法：
        @register_adapter
        class FeishuWebhookAdapter(ChannelAdapter):
            adapter_type = "feishu_webhook"
            ...

    Args:
        adapter_cls: 适配器类（必须定义 adapter_type 类属性）

    Returns:
        注册的适配器类
    """
    adapter_type = adapter_cls.adapter_type
    _ADAPTER_REGISTRY[adapter_type] = adapter_cls
    return adapter_cls


def get_adapter(adapter_type: str) -> ChannelAdapter:
    """获取渠道适配器实例。

    Args:
        adapter_type: 渠道类型标识

    Returns:
        ChannelAdapter 实例

    Raises:
        ValueError: 不支持的渠道类型
    """
    if adapter_type not in _ADAPTER_REGISTRY:
        raise ValueError(
            f"不支持的渠道类型: {adapter_type}，已注册: {list(_ADAPTER_REGISTRY.keys())}"
        )
    return _ADAPTER_REGISTRY[adapter_type]()


def list_supported_adapters() -> list[str]:
    """列出已注册的渠道类型。"""
    return list(_ADAPTER_REGISTRY.keys())


class MockChannelAdapter(ChannelAdapter):
    """Mock 渠道适配器 - 用于测试与开发环境。

    不实际发送消息，仅记录调用并返回成功。
    """

    adapter_type: ClassVar[str] = "mock"

    async def send(
        self,
        message_dto: NotificationMessageDTO,
        channel_config: dict[str, Any],
    ) -> DeliveryResult:
        """Mock 投递 - 总是返回成功。"""
        return DeliveryResult(
            success=True,
            provider_response={"mock": True, "title": message_dto.title},
        )

    async def verify(self, channel_config: dict[str, Any]) -> bool:
        """Mock 验证 - 总是返回 True。"""
        return True

    async def send_image_bytes(
        self,
        image_bytes: bytes,
        channel_config: dict[str, Any],
    ) -> DeliveryResult:
        """Mock 图片投递 - 总是返回成功。"""
        return DeliveryResult(
            success=True,
            provider_response={"mock": True, "image_size": len(image_bytes)},
        )

    async def send_text_message(
        self,
        message_dto: NotificationMessageDTO,
        channel_config: dict[str, Any],
    ) -> DeliveryResult:
        """Mock 纯文本投递 - 总是返回成功。"""
        return DeliveryResult(
            success=True,
            provider_response={"mock": True, "text_content": message_dto.text_content},
        )


# 注册 Mock 适配器（开发/测试环境使用）
_ADAPTER_REGISTRY["mock"] = MockChannelAdapter


if __name__ == "__main__":
    # 自测入口：验证适配器注册与获取
    print(f"已注册适配器: {list_supported_adapters()}")
    assert "mock" in list_supported_adapters()

    # 测试 Mock 适配器
    import asyncio

    from app.schemas.notification import NotificationMessageDTO

    adapter = get_adapter("mock")
    print(f"adapter type={adapter.adapter_type}")

    dto = NotificationMessageDTO(
        message_type="SYSTEM_ALERT",
        template_key="system_alert",
        template_version="1.1.0",
        title="测试消息",
        summary="这是一条测试消息",
        resource_refs={"test": "value"},
        data_time="2026-06-18T10:00:00+08:00",
    )

    result = asyncio.run(adapter.send(dto, {"url": "http://example.com"}))
    print(f"send result: success={result.success}")
    assert result.success is True

    verified = asyncio.run(adapter.verify({"url": "http://example.com"}))
    print(f"verify result: {verified}")
    assert verified is True

    # 测试不支持的渠道类型
    try:
        get_adapter("unsupported")
    except ValueError as e:
        print(f"预期错误: {e}")

    print("OK")
