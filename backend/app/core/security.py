"""JWT 安全工具 + 密码哈希 + Secret 对称加密。

提供：
- create_access_token(subject): 创建 access token（默认 30 分钟，由 settings.jwt_access_ttl_seconds 控制）
- create_refresh_token(subject): 创建 refresh token（默认 7 天，由 settings.jwt_refresh_ttl_seconds 控制）
- decode_token(token): 解码并验证 JWT，失败时 re-raise JWTError（不吞没）
- verify_password(plain, hashed): 验证 bcrypt 密码
- get_password_hash(plain): 生成 bcrypt 密码哈希
- encrypt_secret(plain): Fernet 对称加密 Secret 值
- decrypt_secret(cipher): Fernet 解密 Secret 值

设计说明：
- JWT 使用 HS256（settings.jwt_algorithm），密钥来自 settings.jwt_secret
- 密码哈希直接使用 bcrypt 库（passlib 1.7.4 与 bcrypt 5.x 存在兼容性问题，
  直接调用 bcrypt.hashpw/checkpw 避免该问题；bcrypt 自动处理盐与版本）
- bcrypt 限制密码最长 72 字节，超过则截断到前 72 字节（与 passlib 默认行为一致）
- Secret 加密使用 cryptography.Fernet（对称加密，可逆），密钥从 settings.secret_master_key
  经 SHA256 派生为 32 字节后 base64 编码（Fernet 要求 urlsafe base64 32 字节密钥）
- 所有异常均补充上下文后 re-raise，禁止吞没
"""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
from cryptography.fernet import Fernet, InvalidToken
from jose import JWTError, jwt

from app.config import get_settings

_settings = get_settings()

# bcrypt 密码最大长度限制（72 字节，超出截断）
_BCRYPT_MAX_BYTES = 72


def _truncate_for_bcrypt(plain: str) -> bytes:
    """将密码编码为 utf-8 并截断到 72 字节（bcrypt 限制）。

    Args:
        plain: 明文密码

    Returns:
        utf-8 编码的字节串，最多 72 字节
    """
    raw = plain.encode("utf-8")
    return raw[:_BCRYPT_MAX_BYTES]


def get_password_hash(plain: str) -> str:
    """生成 bcrypt 密码哈希。

    使用 bcrypt.gensalt() 生成盐（默认 12 轮），bcrypt.hashpw() 计算哈希。
    密码超过 72 字节时截断（bcrypt 限制）。

    Args:
        plain: 明文密码

    Returns:
        bcrypt 哈希字符串（utf-8 解码，含盐与版本前缀 $2b$）

    Raises:
        ValueError: 密码为空或哈希失败时抛出（不吞没）
    """
    if not plain:
        raise ValueError("密码不能为空")
    try:
        salt = bcrypt.gensalt()
        hashed = bcrypt.hashpw(_truncate_for_bcrypt(plain), salt)
        return hashed.decode("utf-8")
    except Exception as e:
        raise ValueError(f"密码哈希失败: {e}") from e


def verify_password(plain: str, hashed: str) -> bool:
    """验证明文密码与 bcrypt 哈希是否匹配。

    使用 bcrypt.checkpw() 进行常量时间比较，避免时序攻击。

    Args:
        plain: 明文密码
        hashed: bcrypt 哈希字符串

    Returns:
        True 表示匹配，False 表示不匹配

    Raises:
        ValueError: 哈希格式非法时抛出（不吞没）
    """
    if not plain or not hashed:
        raise ValueError("明文密码与哈希均不能为空")
    try:
        return bcrypt.checkpw(
            _truncate_for_bcrypt(plain), hashed.encode("utf-8")
        )
    except ValueError as e:
        # 哈希格式错误（如非 bcrypt 哈希），补上下文后 re-raise
        raise ValueError(f"密码验证失败（哈希格式可能非法）: {e}") from e
    except Exception as e:
        raise ValueError(f"密码验证失败: {e}") from e


def create_access_token(
    subject: str,
    extra_claims: dict[str, Any] | None = None,
    expires_delta: timedelta | None = None,
) -> str:
    """创建 JWT access token。

    Args:
        subject: 用户标识（user_id 字符串）
        extra_claims: 额外声明（如角色）
        expires_delta: 过期时间增量，默认使用配置项 jwt_access_ttl_seconds

    Returns:
        编码后的 JWT 字符串
    """
    expire = datetime.now(UTC) + (
        expires_delta or timedelta(seconds=_settings.jwt_access_ttl_seconds)
    )
    payload: dict[str, Any] = {
        "sub": subject,
        "exp": expire,
        "type": "access",
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, _settings.jwt_secret, algorithm=_settings.jwt_algorithm)


def create_refresh_token(
    subject: str,
    expires_delta: timedelta | None = None,
) -> str:
    """创建 JWT refresh token。

    Args:
        subject: 用户标识（user_id 字符串）
        expires_delta: 过期时间增量，默认使用配置项 jwt_refresh_ttl_seconds

    Returns:
        编码后的 JWT 字符串
    """
    expire = datetime.now(UTC) + (
        expires_delta or timedelta(seconds=_settings.jwt_refresh_ttl_seconds)
    )
    payload: dict[str, Any] = {
        "sub": subject,
        "exp": expire,
        "type": "refresh",
    }
    return jwt.encode(payload, _settings.jwt_secret, algorithm=_settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    """解码并验证 JWT token。

    Args:
        token: JWT 字符串

    Returns:
        解码后的 payload（含 sub、exp、type 等声明）

    Raises:
        JWTError: token 无效或过期时抛出（不吞没异常）
    """
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            _settings.jwt_secret,
            algorithms=[_settings.jwt_algorithm],
        )
    except JWTError as e:
        # 补充上下文后 re-raise，禁止吞没
        raise JWTError(f"JWT 解码失败: {e}") from e
    return payload


def _derive_fernet_key() -> bytes:
    """从 settings.secret_master_key 派生 Fernet 密钥。

    Fernet 要求 32 字节密钥经 urlsafe base64 编码。
    使用 SHA256 派生固定 32 字节密钥，确保任意长度的 master_key 都可用。

    Returns:
        urlsafe base64 编码的 32 字节密钥
    """
    digest = hashlib.sha256(_settings.secret_master_key.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_secret(plain: str) -> str:
    """使用 Fernet 对称加密 Secret 值。

    用于配置中心敏感字段（如 webhook 签名密钥）的加密存储。
    密钥从 settings.secret_master_key 派生。

    Args:
        plain: 明文 Secret 值

    Returns:
        Fernet 加密后的密文字符串（utf-8 解码）

    Raises:
        ValueError: 明文为空或加密失败时抛出（不吞没）
    """
    if plain is None:
        raise ValueError("Secret 明文不能为 None")
    try:
        fernet = Fernet(_derive_fernet_key())
        return fernet.encrypt(plain.encode("utf-8")).decode("utf-8")
    except Exception as e:
        raise ValueError(f"Secret 加密失败: {e}") from e


def decrypt_secret(cipher: str) -> str:
    """使用 Fernet 解密 Secret 值。

    Args:
        cipher: Fernet 加密后的密文字符串

    Returns:
        解密后的明文 Secret 值

    Raises:
        ValueError: 密文为空或解密失败（密钥不匹配/密文损坏）时抛出（不吞没）
    """
    if not cipher:
        raise ValueError("Secret 密文不能为空")
    try:
        fernet = Fernet(_derive_fernet_key())
        return fernet.decrypt(cipher.encode("utf-8")).decode("utf-8")
    except InvalidToken as e:
        raise ValueError(f"Secret 解密失败（密钥不匹配或密文损坏）: {e}") from e
    except Exception as e:
        raise ValueError(f"Secret 解密失败: {e}") from e


if __name__ == "__main__":
    # 自测入口：验证 token 创建/解码、密码哈希、Secret 加解密（无副作用，不写库表）
    # 1. JWT access token
    token = create_access_token(subject="test-user")
    print(f"access_token={token[:30]}...")
    decoded = decode_token(token)
    assert decoded["sub"] == "test-user"
    assert decoded["type"] == "access"
    print(f"decoded.sub={decoded['sub']}, type={decoded['type']}")

    # 2. JWT refresh token
    rtoken = create_refresh_token(subject="test-user")
    rdecoded = decode_token(rtoken)
    assert rdecoded["type"] == "refresh"
    print(f"refresh_token type={rdecoded['type']}")

    # 3. 密码哈希与验证
    hashed = get_password_hash("test-password-123")
    assert verify_password("test-password-123", hashed) is True
    assert verify_password("wrong-password", hashed) is False
    print(f"password_hash prefix={hashed[:20]}...")

    # 4. Secret 加解密
    cipher = encrypt_secret("my-secret-webhook-key")
    assert cipher != "my-secret-webhook-key"
    plain = decrypt_secret(cipher)
    assert plain == "my-secret-webhook-key"
    print("secret encrypt/decrypt round-trip OK")

    print("OK")
