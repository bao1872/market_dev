"""飞书图片投递集成测试 — PROMPT.md §3 要求 5-7。

测试覆盖（mock httpx，不调用真实飞书 API）：
5. image_key 上传成功：upload 返回 code=0 + image_key，adapter 正确提取
6. 图片消息发送成功：send 返回 code=0，DeliveryResult.success=True
7. 幂等重试：RETRYABLE 状态码（429/5xx）返回 error_code=RETRYABLE，上层可重试

补充覆盖：
- image_key 缺失（upload 成功但无 image_key 字段）
- 图片消息发送飞书 code!=0（业务失败）
- channel_config 缺少凭证
- image_bytes 过小
- token 获取失败

设计说明：
- mock _get_tenant_access_token 避免真实鉴权
- mock httpx.AsyncClient 的 post 方法，按调用顺序返回 upload/send 响应
- 不依赖 DB，纯单元测试
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.feishu_platform_app_adapter import FeishuPlatformAppAdapter

# 合成 PNG bytes（send_image_bytes 只校验 len >= 100，不校验 PNG 内容；
# PNG 内容校验由 stock_capture_service 层的 png_validator 负责）
_FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200

_CHANNEL_CONFIG = {
    "app_id": "cli_test_app_id",
    "app_secret": "test_secret",
    "receive_id": "test_user_id",
    "receive_id_type": "user_id",
}


def _make_response(*, status_code: int = 200, json_data: dict | None = None, text: str = ""):
    """构造 mock httpx.Response（同步属性 + 同步 json() 方法）。"""
    # 用 MagicMock 而非 AsyncMock：resp.status_code / resp.text 是同步属性，
    # resp.json() 是同步方法返回 dict（httpx.Response.json 本身就是同步的）
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text or (str(json_data) if json_data else "")
    resp.json.return_value = json_data or {}
    return resp


def _mock_httpx_client(*, upload_resp, send_resp):
    """构造 mock httpx.AsyncClient，post 按调用顺序返回 upload_resp / send_resp。

    send_image_bytes 内部两次 httpx.AsyncClient 调用：
    1. upload POST open.feishu.cn/open-apis/im/v1/images
    2. send POST open.feishu.cn/open-apis/im/v1/messages
    """
    mock_client = AsyncMock()
    responses = [upload_resp, send_resp]
    call_idx = {"i": 0}

    async def _post(*args, **kwargs):
        idx = call_idx["i"]
        call_idx["i"] += 1
        return responses[idx] if idx < len(responses) else responses[-1]

    mock_client.post = _post
    return mock_client


@pytest.fixture
def adapter() -> FeishuPlatformAppAdapter:
    return FeishuPlatformAppAdapter()


@pytest.fixture
def mock_token():
    """mock _get_tenant_access_token 返回 fake token。"""
    with patch(
        "app.services.feishu_platform_app_adapter._get_tenant_access_token",
        new=AsyncMock(return_value=("fake_tenant_token", None)),
    ) as m:
        yield m


class TestImageUploadAndSendSuccess:
    """§3 要求 5-6：image_key 上传成功 + 图片消息发送成功。"""

    async def test_full_success_returns_image_key_and_success(
        self, adapter, mock_token,
    ) -> None:
        """upload 返回 image_key + send 返回 code=0 → success=True, image_key 非空。"""
        upload_resp = _make_response(json_data={
            "code": 0,
            "data": {"image_key": "img_v3_test_key_abc"},
        })
        send_resp = _make_response(json_data={"code": 0, "data": {"message_id": "om_test_msg"}})
        mock_client = _mock_httpx_client(upload_resp=upload_resp, send_resp=send_resp)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await adapter.send_image_bytes(_FAKE_PNG, _CHANNEL_CONFIG)

        assert result.success is True, f"应成功: {result.error_code} {result.error_message}"
        assert result.image_upload_success is True
        assert result.image_key == "img_v3_test_key_abc"
        mock_token.assert_awaited_once_with("cli_test_app_id", "test_secret")


class TestImageKeyMissing:
    """§3 要求 5 异常分支：upload 成功但未返回 image_key。"""

    async def test_upload_success_but_no_image_key(
        self, adapter, mock_token,
    ) -> None:
        """upload 返回 code=0 但 data 无 image_key → IMAGE_KEY_MISSING。"""
        upload_resp = _make_response(json_data={"code": 0, "data": {}})
        send_resp = _make_response(json_data={"code": 0})
        mock_client = _mock_httpx_client(upload_resp=upload_resp, send_resp=send_resp)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await adapter.send_image_bytes(_FAKE_PNG, _CHANNEL_CONFIG)

        assert result.success is False
        assert result.error_code == "IMAGE_KEY_MISSING"
        assert result.image_upload_success is False


class TestRetryableStatus:
    """§3 要求 7：幂等重试 - RETRYABLE 状态码。"""

    @pytest.mark.parametrize("status_code", [429, 500, 502, 503, 504])
    async def test_upload_retryable_returns_retryable(
        self, adapter, mock_token, status_code,
    ) -> None:
        """upload 返回 429/5xx → error_code=RETRYABLE，上层可重试。"""
        upload_resp = _make_response(status_code=status_code, text=f"HTTP {status_code}")
        send_resp = _make_response(json_data={"code": 0})
        mock_client = _mock_httpx_client(upload_resp=upload_resp, send_resp=send_resp)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await adapter.send_image_bytes(_FAKE_PNG, _CHANNEL_CONFIG)

        assert result.success is False
        assert result.error_code == "RETRYABLE"
        assert result.image_upload_success is False

    @pytest.mark.parametrize("status_code", [429, 500, 502, 503, 504])
    async def test_send_retryable_returns_retryable_with_image_key(
        self, adapter, mock_token, status_code,
    ) -> None:
        """upload 成功 + send 返回 429/5xx → RETRYABLE，但 image_upload_success=True, image_key 保留。

        场景：图片已上传成功（image_key 已获得），但消息发送遇到临时错误，可重试发送（无需重新上传）。
        """
        upload_resp = _make_response(json_data={
            "code": 0,
            "data": {"image_key": "img_v3_retry_key"},
        })
        send_resp = _make_response(status_code=status_code, text=f"HTTP {status_code}")
        mock_client = _mock_httpx_client(upload_resp=upload_resp, send_resp=send_resp)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await adapter.send_image_bytes(_FAKE_PNG, _CHANNEL_CONFIG)

        assert result.success is False
        assert result.error_code == "RETRYABLE"
        assert result.image_upload_success is True
        assert result.image_key == "img_v3_retry_key"


class TestSendFeishuCodeNonZero:
    """图片消息发送飞书 code!=0（业务失败）。"""

    async def test_send_feishu_code_nonzero_returns_failure(
        self, adapter, mock_token,
    ) -> None:
        """upload 成功 + send 返回 code!=0 → success=False, image_key 保留。"""
        upload_resp = _make_response(json_data={
            "code": 0,
            "data": {"image_key": "img_v3_code_fail"},
        })
        send_resp = _make_response(json_data={
            "code": 230002,
            "msg": "user is not in contact",
        })
        mock_client = _mock_httpx_client(upload_resp=upload_resp, send_resp=send_resp)

        with patch("httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await adapter.send_image_bytes(_FAKE_PNG, _CHANNEL_CONFIG)

        assert result.success is False
        assert result.error_code == "FEISHU_230002"
        assert result.error_message == "user is not in contact"
        assert result.image_upload_success is True
        assert result.image_key == "img_v3_code_fail"


class TestConfigAndImageValidation:
    """配置缺失 + 图片 bytes 校验。"""

    async def test_config_missing_app_id(self, adapter) -> None:
        """channel_config 缺少 app_id → CONFIG_MISSING。"""
        bad_config = {**_CHANNEL_CONFIG}
        del bad_config["app_id"]
        result = await adapter.send_image_bytes(_FAKE_PNG, bad_config)
        assert result.success is False
        assert result.error_code == "CONFIG_MISSING"
        assert result.image_upload_success is False

    async def test_config_missing_receive_id(self, adapter) -> None:
        """channel_config 缺少 receive_id → CONFIG_MISSING。"""
        bad_config = {**_CHANNEL_CONFIG}
        del bad_config["receive_id"]
        result = await adapter.send_image_bytes(_FAKE_PNG, bad_config)
        assert result.success is False
        assert result.error_code == "CONFIG_MISSING"

    async def test_image_bytes_too_small(self, adapter) -> None:
        """image_bytes < 100 字节 → INVALID_IMAGE。"""
        result = await adapter.send_image_bytes(b"\x89PNG" + b"\x00" * 50, _CHANNEL_CONFIG)
        assert result.success is False
        assert result.error_code == "INVALID_IMAGE"
        assert result.image_upload_success is False

    async def test_image_bytes_empty(self, adapter) -> None:
        """image_bytes 为空 → INVALID_IMAGE。"""
        result = await adapter.send_image_bytes(b"", _CHANNEL_CONFIG)
        assert result.success is False
        assert result.error_code == "INVALID_IMAGE"


class TestAuthFailure:
    """token 获取失败。"""

    async def test_token_failure_returns_auth_failed(
        self, adapter,
    ) -> None:
        """_get_tenant_access_token 失败 → AUTH_FAILED。"""
        with patch(
            "app.services.feishu_platform_app_adapter._get_tenant_access_token",
            new=AsyncMock(return_value=("", "app_secret 无效")),
        ):
            result = await adapter.send_image_bytes(_FAKE_PNG, _CHANNEL_CONFIG)

        assert result.success is False
        assert result.error_code == "AUTH_FAILED"
        assert result.image_upload_success is False


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
