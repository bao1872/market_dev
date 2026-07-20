"""PNG 内容校验器 — 纯 Python + numpy 实现，无新依赖。

PROMPT.md §3 要求飞书图片链路必须校验：
1. PNG 存在
2. 合理尺寸（像素宽高）
3. 合理字节数
4. 非全透明 / 非纯色

本模块用标准库 struct + zlib + 已安装的 numpy 实现 PNG 解码与内容校验，
不引入 Pillow / cv2 等新依赖。

支持 color type:
- 0: Grayscale (1 sample)
- 2: RGB (3 samples)
- 3: Palette (1 sample, 索引到 PLTE)
- 4: Grayscale + Alpha (2 samples)
- 6: RGBA (4 samples)

仅支持 bit depth = 8（Playwright/Chromium 截图的标准输出）。
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

import numpy as np

# PNG signature (8 bytes)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# 校验阈值（PROMPT.md §3）
_MIN_PNG_BYTES = 10_000  # 10KB：真实截图应远大于此
_MIN_WIDTH = 200
_MIN_HEIGHT = 200

# color type → channels 映射
_COLOR_TYPE_CHANNELS: dict[int, int] = {
    0: 1,  # Grayscale
    2: 3,  # RGB
    3: 1,  # Palette (索引)
    4: 2,  # Grayscale + Alpha
    6: 4,  # RGBA
}


class PngValidationError(RuntimeError):
    """PNG 校验失败异常。"""


@dataclass
class PngValidationResult:
    """PNG 校验结果。"""

    valid: bool
    width: int
    height: int
    color_type: int
    channels: int
    byte_size: int
    error_code: str | None = None
    error_message: str | None = None


def _parse_ihdr(data: bytes) -> tuple[int, int, int, int]:
    """解析 IHDR chunk 数据。

    Returns:
        (width, height, bit_depth, color_type)
    """
    if len(data) < 13:
        raise PngValidationError("IHDR_DATA_TOO_SHORT", "IHDR chunk 数据不足 13 字节")
    width, height, bit_depth, color_type = struct.unpack(">IIBB", data[:10])
    return width, height, bit_depth, color_type


def _collect_chunks(png_bytes: bytes) -> tuple[bytes, int, int, int, int, bytes | None]:
    """遍历 PNG chunks，收集 IDAT 数据和 IHDR 元信息。

    Returns:
        (idat_data, width, height, bit_depth, color_type, plte_data)
    """
    if len(png_bytes) < 8 or png_bytes[:8] != _PNG_SIGNATURE:
        raise PngValidationError("INVALID_SIGNATURE", "PNG signature 不匹配")

    offset = 8
    idat_chunks: list[bytes] = []
    width = height = bit_depth = color_type = 0
    plte_data: bytes | None = None

    while offset < len(png_bytes):
        if offset + 8 > len(png_bytes):
            break
        chunk_length = struct.unpack(">I", png_bytes[offset : offset + 4])[0]
        chunk_type = png_bytes[offset + 4 : offset + 8]
        chunk_data_start = offset + 8
        chunk_data_end = chunk_data_start + chunk_length

        if chunk_data_end > len(png_bytes):
            raise PngValidationError(
                "CHUNK_TRUNCATED",
                f"chunk {chunk_type.decode('ascii', errors='replace')} 数据截断",
            )

        chunk_data = png_bytes[chunk_data_start:chunk_data_end]

        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type = _parse_ihdr(chunk_data)
        elif chunk_type == b"PLTE":
            plte_data = chunk_data
        elif chunk_type == b"IDAT":
            idat_chunks.append(chunk_data)
        elif chunk_type == b"IEND":
            break

        # 跳过 data + 4字节 CRC
        offset = chunk_data_end + 4

    if width == 0 or height == 0:
        raise PngValidationError("IHDR_MISSING", "未找到 IHDR chunk 或尺寸为 0")
    if not idat_chunks:
        raise PngValidationError("IDAT_MISSING", "未找到 IDAT chunk")

    return b"".join(idat_chunks), width, height, bit_depth, color_type, plte_data


def _reverse_filter(
    raw_data: bytes, width: int, height: int, channels: int
) -> np.ndarray:
    """反向 PNG filter，重建像素数组。

    PNG 每行第一个字节是 filter type (0-4)，后面是像素数据。
    支持 filter type: 0(None) / 1(Sub) / 2(Up) / 3(Average) / 4(Paeth)

    Returns:
        numpy 数组，shape=(height, width, channels)，dtype=uint8
    """
    bpp = channels  # bytes per pixel (bit depth = 8)
    stride = width * bpp  # 每行像素字节数（不含 filter byte）
    expected_len = height * (1 + stride)
    if len(raw_data) != expected_len:
        raise PngValidationError(
            "IDAT_LENGTH_MISMATCH",
            f"解压后数据长度 {len(raw_data)} != 预期 {expected_len}",
        )

    arr = np.frombuffer(raw_data, dtype=np.uint8)
    pixels = np.zeros((height, width, channels), dtype=np.uint8)

    prev_row = np.zeros(stride, dtype=np.uint8)

    for y in range(height):
        row_start = y * (1 + stride)
        filter_type = arr[row_start]
        row_data = arr[row_start + 1 : row_start + 1 + stride].copy()

        if filter_type == 0:
            # None
            pass
        elif filter_type == 1:
            # Sub: recon(x) = filt(x) + recon(a)  (a = 左侧 bpp 字节)
            # 显式 int() 避免 numpy uint8 加法溢出 RuntimeWarning
            for i in range(bpp, stride):
                row_data[i] = (int(row_data[i]) + int(row_data[i - bpp])) & 0xFF
        elif filter_type == 2:
            # Up: recon(x) = filt(x) + recon(b)  (b = 上方同位置)
            # 用 int16 中间类型避免 uint8 溢出警告，最后 & 0xFF 再转回 uint8
            row_data = ((row_data.astype(np.int16) + prev_row.astype(np.int16)) & 0xFF).astype(
                np.uint8
            )
        elif filter_type == 3:
            # Average: recon(x) = filt(x) + floor((a + b) / 2)
            for i in range(stride):
                a = int(row_data[i - bpp]) if i >= bpp else 0
                b = int(prev_row[i])
                row_data[i] = (int(row_data[i]) + ((a + b) // 2)) & 0xFF
        elif filter_type == 4:
            # Paeth: recon(x) = filt(x) + PaethPredictor(a, b, c)
            for i in range(stride):
                a = int(row_data[i - bpp]) if i >= bpp else 0
                b = int(prev_row[i])
                c = int(prev_row[i - bpp]) if i >= bpp else 0
                p = a + b - c
                pa = abs(p - a)
                pb = abs(p - b)
                pc = abs(p - c)
                if pa <= pb and pa <= pc:
                    pred = a
                elif pb <= pc:
                    pred = b
                else:
                    pred = c
                row_data[i] = (int(row_data[i]) + pred) & 0xFF
        else:
            raise PngValidationError(
                "UNKNOWN_FILTER_TYPE",
                f"行 {y} 未知 filter type: {filter_type}",
            )

        pixels[y] = row_data.reshape(width, channels)
        prev_row = row_data

    return pixels


def _paeth_predictor(a: int, b: int, c: int) -> int:
    """Paeth 预测器（标准 PNG 定义）。"""
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def validate_png(png_bytes: bytes) -> PngValidationResult:
    """校验 PNG 字节流，返回 PngValidationResult。

    校验项（PROMPT.md §3 要求 1-4）：
    1. PNG 存在（signature + IHDR + IDAT）
    2. 合理尺寸（width >= 200, height >= 200）
    3. 非全透明 / 非纯色（像素内容有变化）
    4. 合理字节数（>= 10KB，纯色 PNG 因 zlib 压缩后总是很小会被内容校验先行拒绝）

    校验顺序设计：
    - 早期最小字节数检查（>= 100 字节）防止空文件/损坏文件
    - PNG signature + IHDR + 尺寸检查
    - IDAT 解压 + 内容检查（非纯色/非全透明）
    - 后期合理字节数检查（>= 10KB）防止过小截图

    Args:
        png_bytes: PNG 图片字节流

    Returns:
        PngValidationResult：valid=True 时可通过；valid=False 时查看 error_code/message
    """
    byte_size = len(png_bytes)

    # 1. 早期最小字节数检查（防止空文件/损坏文件）
    if byte_size < 100:
        return PngValidationResult(
            valid=False,
            width=0,
            height=0,
            color_type=0,
            channels=0,
            byte_size=byte_size,
            error_code="PNG_TOO_SMALL",
            error_message=f"PNG 字节数 {byte_size} < 最小阈值 100",
        )

    # 2. 解析 chunks
    try:
        idat_data, width, height, bit_depth, color_type, plte_data = _collect_chunks(
            png_bytes
        )
    except PngValidationError as e:
        return PngValidationResult(
            valid=False,
            width=0,
            height=0,
            color_type=0,
            channels=0,
            byte_size=byte_size,
            error_code=e.args[0] if e.args else "PARSE_ERROR",
            error_message=str(e),
        )

    # 3. 尺寸校验
    if width < _MIN_WIDTH or height < _MIN_HEIGHT:
        return PngValidationResult(
            valid=False,
            width=width,
            height=height,
            color_type=color_type,
            channels=0,
            byte_size=byte_size,
            error_code="DIMENSIONS_TOO_SMALL",
            error_message=f"PNG 尺寸 {width}x{height} < 阈值 {_MIN_WIDTH}x{_MIN_HEIGHT}",
        )

    # 4. bit depth 校验（仅支持 8）
    if bit_depth != 8:
        return PngValidationResult(
            valid=False,
            width=width,
            height=height,
            color_type=color_type,
            channels=0,
            byte_size=byte_size,
            error_code="UNSUPPORTED_BIT_DEPTH",
            error_message=f"bit depth {bit_depth} 不支持（仅支持 8）",
        )

    # 5. color type 校验
    channels = _COLOR_TYPE_CHANNELS.get(color_type)
    if channels is None:
        return PngValidationResult(
            valid=False,
            width=width,
            height=height,
            color_type=color_type,
            channels=0,
            byte_size=byte_size,
            error_code="UNSUPPORTED_COLOR_TYPE",
            error_message=f"color type {color_type} 不支持",
        )

    # 6. 解压 IDAT
    try:
        raw_data = zlib.decompress(idat_data)
    except zlib.error as e:
        return PngValidationResult(
            valid=False,
            width=width,
            height=height,
            color_type=color_type,
            channels=channels,
            byte_size=byte_size,
            error_code="IDAT_DECOMPRESS_FAILED",
            error_message=f"zlib 解压失败: {e}",
        )

    # 7. 反向 filter，重建像素
    try:
        pixels = _reverse_filter(raw_data, width, height, channels)
    except PngValidationError as e:
        return PngValidationResult(
            valid=False,
            width=width,
            height=height,
            color_type=color_type,
            channels=channels,
            byte_size=byte_size,
            error_code=e.args[0] if e.args else "REVERSE_FILTER_FAILED",
            error_message=str(e),
        )

    # 8. 内容校验：非全透明 / 非纯色
    has_alpha = color_type in (4, 6)  # Grayscale+Alpha / RGBA
    if has_alpha:
        alpha = pixels[:, :, -1]
        if np.all(alpha == 0):
            return PngValidationResult(
                valid=False,
                width=width,
                height=height,
                color_type=color_type,
                channels=channels,
                byte_size=byte_size,
                error_code="ALL_TRANSPARENT",
                error_message="PNG 所有像素 alpha=0（全透明）",
            )

    # 检查非纯色（忽略 alpha channel，只看颜色通道）
    color_channels = pixels[:, :, :3] if channels >= 3 else pixels[:, :, :1]
    first_pixel = color_channels[0, 0]
    if np.all(color_channels == first_pixel):
        return PngValidationResult(
            valid=False,
            width=width,
            height=height,
            color_type=color_type,
            channels=channels,
            byte_size=byte_size,
            error_code="SOLID_COLOR",
            error_message=f"PNG 所有像素颜色相同（纯色）: RGB={tuple(first_pixel.tolist())}",
        )

    # 9. 后期合理字节数检查（内容已通过，但文件过小可能表示截图异常）
    if byte_size < _MIN_PNG_BYTES:
        return PngValidationResult(
            valid=False,
            width=width,
            height=height,
            color_type=color_type,
            channels=channels,
            byte_size=byte_size,
            error_code="PNG_TOO_SMALL",
            error_message=f"PNG 字节数 {byte_size} < 合理阈值 {_MIN_PNG_BYTES}",
        )

    return PngValidationResult(
        valid=True,
        width=width,
        height=height,
        color_type=color_type,
        channels=channels,
        byte_size=byte_size,
    )


def validate_png_or_raise(png_bytes: bytes) -> PngValidationResult:
    """校验 PNG，失败时抛出 PngValidationError。"""
    result = validate_png(png_bytes)
    if not result.valid:
        raise PngValidationError(result.error_code, result.error_message)
    return result


if __name__ == "__main__":
    # 自测：生成一个简单的纯色 PNG 和一个有内容的 PNG

    # 生成一个 300x200 的纯色 RGBA PNG（手动构造）
    def make_solid_png(width: int, height: int, r: int, g: int, b: int, a: int = 255) -> bytes:
        """构造一个纯色 PNG。"""
        def chunk(ctype: bytes, data: bytes) -> bytes:
            length = struct.pack(">I", len(data))
            crc = struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
            return length + ctype + data + crc

        ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
        raw = b""
        for _ in range(height):
            raw += b"\x00"  # filter type None
            raw += bytes([r, g, b, a]) * width
        idat = zlib.compress(raw)
        return _PNG_SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")

    def make_gradient_png(width: int, height: int) -> bytes:
        """构造一个有渐变内容的 PNG。"""
        def chunk(ctype: bytes, data: bytes) -> bytes:
            length = struct.pack(">I", len(data))
            crc = struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
            return length + ctype + data + crc

        ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
        raw = b""
        for y in range(height):
            raw += b"\x00"  # filter type None
            for x in range(width):
                r = (x * 255) // width
                g = (y * 255) // height
                b = 128
                raw += bytes([r, g, b, 255])
        idat = zlib.compress(raw)
        return _PNG_SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")

    # 测试纯色 PNG（应失败）
    solid = make_solid_png(300, 200, 100, 150, 200)
    result = validate_png(solid)
    assert not result.valid, f"纯色 PNG 应校验失败: {result}"
    assert result.error_code == "SOLID_COLOR", f"错误码应为 SOLID_COLOR: {result.error_code}"
    print(f"纯色 PNG 校验失败 OK: {result.error_code}")

    # 测试全透明 PNG（应失败）
    transparent = make_solid_png(300, 200, 100, 150, 200, a=0)
    result = validate_png(transparent)
    assert not result.valid, f"全透明 PNG 应校验失败: {result}"
    assert result.error_code == "ALL_TRANSPARENT", f"错误码应为 ALL_TRANSPARENT: {result.error_code}"
    print(f"全透明 PNG 校验失败 OK: {result.error_code}")

    # 测试尺寸过小 PNG（应失败）
    small = make_solid_png(100, 100, 100, 150, 200)
    result = validate_png(small)
    assert not result.valid, f"小尺寸 PNG 应校验失败: {result}"
    assert result.error_code == "DIMENSIONS_TOO_SMALL", f"错误码应为 DIMENSIONS_TOO_SMALL: {result.error_code}"
    print(f"小尺寸 PNG 校验失败 OK: {result.error_code}")

    # 测试字节数过小（应失败，< 100 字节早期检查）
    result = validate_png(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
    assert not result.valid, f"过小 PNG 应校验失败: {result}"
    assert result.error_code == "PNG_TOO_SMALL", f"错误码应为 PNG_TOO_SMALL: {result.error_code}"
    print(f"过小 PNG 校验失败 OK: {result.error_code}")

    # 测试渐变 PNG（应成功，500x500 渐变内容字节数 >= 10KB）
    gradient = make_gradient_png(500, 500)
    result = validate_png(gradient)
    assert result.valid, f"渐变 PNG 应校验成功: {result}"
    assert result.width == 500 and result.height == 500, f"尺寸不匹配: {result}"
    print(f"渐变 PNG 校验成功 OK: {result.width}x{result.height} color_type={result.color_type} bytes={result.byte_size}")

    # 测试无效 signature（应失败）
    result = validate_png(b"NOT_PNG" + b"\x00" * 10000)
    assert not result.valid, f"无效 signature 应校验失败: {result}"
    print(f"无效 signature 校验失败 OK: {result.error_code}")

    print("ALL TESTS PASSED")
