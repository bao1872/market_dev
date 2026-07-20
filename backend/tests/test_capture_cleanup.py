"""截图临时文件清理服务测试 — PROMPT.md §3 要求 8。

测试覆盖：
1. cleanup_capture_static_dir: 按时间清理过期文件
2. cleanup_old_captures: 按 keep_count 保留最近文件
3. cache 子目录按 TTL 清理
4. 文件不存在/目录不存在的安全处理
"""

from __future__ import annotations

import os
import time

import pytest

from app.services.capture_cleanup import (
    cleanup_capture_static_dir,
    cleanup_old_captures,
)


@pytest.fixture
def temp_capture_dir(tmp_path, monkeypatch):
    """临时替换 _CAPTURE_STATIC_DIR 为 tmp_path。"""
    monkeypatch.setattr(
        "app.services.capture_cleanup._CAPTURE_STATIC_DIR",
        str(tmp_path),
    )
    return str(tmp_path)


def _create_png(path: str, *, mtime_offset: float = 0) -> None:
    """创建一个假 PNG 文件并设置 mtime。"""
    with open(path, "wb") as f:
        f.write(b"fake_png_data" * 100)
    if mtime_offset != 0:
        now = time.time()
        os.utime(path, (now + mtime_offset, now + mtime_offset))


class TestCleanupCaptureStaticDir:
    """cleanup_capture_static_dir 测试。"""

    def test_removes_expired_main_dir_files(self, temp_capture_dir) -> None:
        """主目录中超过 max_age_seconds 的文件应被删除。"""
        now = time.time()
        # 过期文件（2 小时前）
        _create_png(os.path.join(temp_capture_dir, "old.png"), mtime_offset=-7200)
        # 新文件（10 分钟前）
        _create_png(os.path.join(temp_capture_dir, "new.png"), mtime_offset=-600)

        stats = cleanup_capture_static_dir(max_age_seconds=3600, now=now)

        assert stats["removed"] == 1
        assert stats["skipped"] == 1
        assert not os.path.exists(os.path.join(temp_capture_dir, "old.png"))
        assert os.path.exists(os.path.join(temp_capture_dir, "new.png"))

    def test_removes_expired_cache_files(self, temp_capture_dir) -> None:
        """cache 子目录中超过 cache_ttl_seconds 的文件应被删除。"""
        cache_dir = os.path.join(temp_capture_dir, "cache")
        os.makedirs(cache_dir)

        now = time.time()
        # 过期缓存（20 分钟前）
        _create_png(os.path.join(cache_dir, "cache_old.png"), mtime_offset=-1200)
        # 新缓存（5 分钟前）
        _create_png(os.path.join(cache_dir, "cache_new.png"), mtime_offset=-300)

        stats = cleanup_capture_static_dir(cache_ttl_seconds=600, now=now)

        assert stats["removed"] == 1
        assert stats["skipped"] == 1
        assert not os.path.exists(os.path.join(cache_dir, "cache_old.png"))
        assert os.path.exists(os.path.join(cache_dir, "cache_new.png"))

    def test_skips_non_png_files(self, temp_capture_dir) -> None:
        """非 PNG 文件应被跳过。"""
        now = time.time()
        # 非 PNG 文件
        with open(os.path.join(temp_capture_dir, "readme.txt"), "w") as f:
            f.write("not a png")
        os.utime(os.path.join(temp_capture_dir, "readme.txt"), (now - 7200, now - 7200))

        stats = cleanup_capture_static_dir(max_age_seconds=3600, now=now)

        assert stats["removed"] == 0
        assert os.path.exists(os.path.join(temp_capture_dir, "readme.txt"))

    def test_handles_nonexistent_dir(self, monkeypatch) -> None:
        """目录不存在时应安全返回。"""
        monkeypatch.setattr(
            "app.services.capture_cleanup._CAPTURE_STATIC_DIR",
            "/nonexistent/path/that/does/not/exist",
        )
        stats = cleanup_capture_static_dir()
        assert stats["removed"] == 0
        assert stats["skipped"] == 0
        assert stats["errors"] == 0

    def test_empty_dir(self, temp_capture_dir) -> None:
        """空目录应安全处理。"""
        stats = cleanup_capture_static_dir()
        assert stats["removed"] == 0
        assert stats["skipped"] == 0


class TestCleanupOldCaptures:
    """cleanup_old_captures 测试。"""

    def test_keeps_newest_files(self, temp_capture_dir) -> None:
        """应保留最近 keep_count 个文件。"""
        # 创建 5 个文件，mtime 递减
        for i in range(5):
            path = os.path.join(temp_capture_dir, f"file_{i}.png")
            _create_png(path, mtime_offset=-i * 60)

        stats = cleanup_old_captures(keep_count=3)

        assert stats["kept"] == 3
        assert stats["removed"] == 2
        # 保留 file_0, file_1, file_2（最新）
        assert os.path.exists(os.path.join(temp_capture_dir, "file_0.png"))
        assert os.path.exists(os.path.join(temp_capture_dir, "file_1.png"))
        assert os.path.exists(os.path.join(temp_capture_dir, "file_2.png"))
        # 删除 file_3, file_4（最旧）
        assert not os.path.exists(os.path.join(temp_capture_dir, "file_3.png"))
        assert not os.path.exists(os.path.join(temp_capture_dir, "file_4.png"))

    def test_keep_count_larger_than_files(self, temp_capture_dir) -> None:
        """keep_count > 文件数时应保留全部。"""
        for i in range(3):
            _create_png(os.path.join(temp_capture_dir, f"file_{i}.png"))

        stats = cleanup_old_captures(keep_count=10)

        assert stats["kept"] == 3
        assert stats["removed"] == 0

    def test_skips_cache_subdir(self, temp_capture_dir) -> None:
        """不应清理 cache 子目录。"""
        cache_dir = os.path.join(temp_capture_dir, "cache")
        os.makedirs(cache_dir)
        _create_png(os.path.join(cache_dir, "cache_file.png"))

        cleanup_old_captures(keep_count=0)

        # cache 子目录中的文件不应被清理
        assert os.path.exists(os.path.join(cache_dir, "cache_file.png"))

    def test_skips_non_png_files(self, temp_capture_dir) -> None:
        """非 PNG 文件应被跳过。"""
        with open(os.path.join(temp_capture_dir, "notes.txt"), "w") as f:
            f.write("not a png")

        stats = cleanup_old_captures(keep_count=0)

        assert stats["kept"] == 0
        assert stats["removed"] == 0
        assert os.path.exists(os.path.join(temp_capture_dir, "notes.txt"))


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
