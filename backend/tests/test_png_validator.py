"""PNG 校验器单元测试 — PROMPT.md §3 要求 1-4。

测试覆盖：
1. PNG 存在（signature + IHDR + IDAT）
2. 合理尺寸（width >= 200, height >= 200）
3. 合理字节数（>= 10KB）
4. 非全透明 / 非纯色
5. 各种错误码正确返回
"""

from __future__ import annotations

import struct
import zlib

import pytest

from app.services.png_validator import (
    _PNG_SIGNATURE,
    PngValidationError,
    validate_png,
    validate_png_or_raise,
)


def _make_png(
    width: int,
    height: int,
    color_type: int = 6,
    bit_depth: int = 8,
    pixel_func=None,
    alpha: int = 255,
) -> bytes:
    """构造 PNG bytes。

    Args:
        width: 宽度
        height: 高度
        color_type: 0=Grayscale, 2=RGB, 4=Gray+Alpha, 6=RGBA
        bit_depth: 位深（仅支持 8）
        pixel_func: 自定义像素生成函数 (x, y) -> tuple
        alpha: 默认 alpha 值（当 pixel_func 为 None 时）
    """

    def chunk(ctype: bytes, data: bytes) -> bytes:
        length = struct.pack(">I", len(data))
        crc = struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
        return length + ctype + data + crc

    ihdr = struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, 0)
    raw = b""
    for y in range(height):
        raw += b"\x00"  # filter type None
        for x in range(width):
            if pixel_func:
                px = pixel_func(x, y)
            else:
                if color_type == 6:
                    px = (100, 150, 200, alpha)
                elif color_type == 2:
                    px = (100, 150, 200)
                elif color_type == 4:
                    px = (100, alpha)
                elif color_type == 0:
                    px = (100,)
            raw += bytes(px)
    idat = zlib.compress(raw)
    return _PNG_SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _make_solid_png(width: int, height: int, r: int = 100, g: int = 150, b: int = 200, a: int = 255) -> bytes:
    """构造纯色 RGBA PNG。"""
    return _make_png(width, height, color_type=6, alpha=a)


def _make_gradient_png(width: int, height: int) -> bytes:
    """构造渐变 RGBA PNG。"""
    def pixel_func(x, y):
        return ((x * 255) // width, (y * 255) // height, 128, 255)
    return _make_png(width, height, color_type=6, pixel_func=pixel_func)


def _make_all_transparent_png(width: int, height: int) -> bytes:
    """构造全透明 RGBA PNG。"""
    return _make_png(width, height, color_type=6, alpha=0)


class TestPngValidationSuccess:
    """PNG 校验成功场景。"""

    def test_valid_gradient_png_passes(self) -> None:
        """渐变 PNG 应校验成功。"""
        png = _make_gradient_png(500, 500)
        result = validate_png(png)
        assert result.valid, f"应校验成功: {result.error_code} {result.error_message}"
        assert result.width == 500
        assert result.height == 500
        assert result.color_type == 6
        assert result.channels == 4
        assert result.byte_size == len(png)

    def test_valid_rgb_png_passes(self) -> None:
        """RGB PNG（color_type=2）应校验成功。"""
        def pixel_func(x, y):
            return ((x * 255) // 500, (y * 255) // 500, 128)
        png = _make_png(500, 500, color_type=2, pixel_func=pixel_func)
        result = validate_png(png)
        assert result.valid, f"RGB PNG 应校验成功: {result.error_code}"
        assert result.color_type == 2
        assert result.channels == 3

    def test_validate_png_or_raise_success(self) -> None:
        """validate_png_or_raise 成功时不抛异常。"""
        png = _make_gradient_png(500, 500)
        result = validate_png_or_raise(png)
        assert result.valid


class TestPngValidationFailures:
    """PNG 校验失败场景。"""

    def test_too_small_bytes_rejected(self) -> None:
        """字节数 < 100 应返回 PNG_TOO_SMALL。"""
        result = validate_png(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
        assert not result.valid
        assert result.error_code == "PNG_TOO_SMALL"

    def test_invalid_signature_rejected(self) -> None:
        """无效 PNG signature 应返回 INVALID_SIGNATURE。"""
        result = validate_png(b"NOT_PNG" + b"\x00" * 10000)
        assert not result.valid
        assert result.error_code == "INVALID_SIGNATURE"

    def test_dimensions_too_small_rejected(self) -> None:
        """尺寸 < 200x200 应返回 DIMENSIONS_TOO_SMALL。"""
        png = _make_gradient_png(100, 100)
        result = validate_png(png)
        assert not result.valid
        assert result.error_code == "DIMENSIONS_TOO_SMALL"
        assert result.width == 100
        assert result.height == 100

    def test_solid_color_rejected(self) -> None:
        """纯色 PNG 应返回 SOLID_COLOR。"""
        png = _make_solid_png(300, 300)
        result = validate_png(png)
        assert not result.valid
        assert result.error_code == "SOLID_COLOR"

    def test_all_transparent_rejected(self) -> None:
        """全透明 PNG 应返回 ALL_TRANSPARENT。"""
        png = _make_all_transparent_png(300, 300)
        result = validate_png(png)
        assert not result.valid
        assert result.error_code == "ALL_TRANSPARENT"

    def test_unsupported_bit_depth_rejected(self) -> None:
        """bit_depth != 8 应返回 UNSUPPORTED_BIT_DEPTH。"""
        png = _make_gradient_png(300, 300)
        # IHDR chunk 结构：8字节 signature + 4字节 length + 4字节 "IHDR" + 13字节 data
        # data: width(4) + height(4) + bit_depth(1) + color_type(1) + ...
        # bit_depth 在 offset 8+4+4+4+4 = 24
        png_bad = bytearray(png)
        png_bad[24] = 16  # bit_depth=16
        result = validate_png(bytes(png_bad))
        assert not result.valid
        assert result.error_code == "UNSUPPORTED_BIT_DEPTH"

    def test_validate_png_or_raise_raises(self) -> None:
        """validate_png_or_raise 失败时抛出 PngValidationError。"""
        with pytest.raises(PngValidationError, match="SOLID_COLOR"):
            validate_png_or_raise(_make_solid_png(300, 300))


class TestPngFilterDecoding:
    """PNG filter 解码测试（验证反向 filter 正确性）。"""

    def test_filter_none_decodes_correctly(self) -> None:
        """filter type 0 (None) 的 PNG 应正确解码。"""
        # 渐变 PNG 使用 filter type 0
        png = _make_gradient_png(300, 300)
        result = validate_png(png)
        assert result.valid, f"filter None 应正确解码: {result.error_code}"

    def test_filter_up_decodes_correctly(self) -> None:
        """filter type 2 (Up) 的 PNG 应正确解码。"""
        import random
        random.seed(42)  # 固定种子确保可复现
        width, height = 500, 500
        channels = 4

        def chunk(ctype: bytes, data: bytes) -> bytes:
            length = struct.pack(">I", len(data))
            crc = struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
            return length + ctype + data + crc

        ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
        stride = width * channels
        raw = b""
        prev_row = bytes(stride)  # 第一行 prev_row 全 0
        for y in range(height):
            # 用随机噪声 + 渐变混合，确保 zlib 压缩后字节数 >= 10KB
            row = bytearray()
            for x in range(width):
                r = ((x * 255) // width + random.randint(0, 30)) & 0xFF
                g = ((y * 255) // height + random.randint(0, 30)) & 0xFF
                b = (128 + random.randint(0, 30)) & 0xFF
                row.extend([r, g, b, 255])
            # Up filter: filt = recon - prev_row
            filt = bytearray([2])  # filter type Up
            for i in range(stride):
                filt.append((row[i] - prev_row[i]) & 0xFF)
            raw += bytes(filt)
            prev_row = bytes(row)  # 下一行的 prev_row 是当前行的 recon
        idat = zlib.compress(raw)
        png = _PNG_SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
        result = validate_png(png)
        assert result.valid, f"filter Up 应正确解码: {result.error_code} {result.error_message} bytes={result.byte_size}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
