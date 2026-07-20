"""截图临时文件清理服务 — PROMPT.md §3 要求 8。

清理范围：
1. /app/static/captures/ 目录中的过期 PNG 文件（非 cache 子目录）
2. /app/static/captures/cache/ 目录中的过期缓存文件

清理策略：
- 默认保留 1 小时内的文件，超过的删除
- cache 目录按 _CACHE_TTL_SECONDS（600s）清理
- 不删除正在被引用的文件（通过 mtime 判断）
- 失败仅记录日志，不阻塞主流程

调用方式：
- 由 delivery_worker 或定时任务调用 cleanup_capture_static_dir()
- 或在截图成功后调用 cleanup_old_captures(keep_count=10)
"""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger("capture_cleanup")

# 默认清理阈值
_DEFAULT_MAX_AGE_SECONDS = 3600  # 1 小时
_CACHE_TTL_SECONDS = 600  # 与 stock_capture_service._CACHE_TTL_SECONDS 一致

# 截图静态目录（与 capture_main.py 一致）
_CAPTURE_STATIC_DIR = os.getenv("CAPTURE_STATIC_DIR", "/app/static/captures")
_CACHE_SUBDIR = "cache"


def _safe_remove(path: str, *, reason: str) -> bool:
    """安全删除文件，失败仅记录日志。"""
    try:
        os.unlink(path)
        logger.info("清理截图文件: %s reason=%s", path, reason)
        return True
    except FileNotFoundError:
        return True  # 文件已不存在，视为成功
    except OSError as exc:
        logger.warning("清理截图文件失败: %s reason=%s error=%s", path, reason, exc)
        return False


def cleanup_capture_static_dir(
    *,
    max_age_seconds: int = _DEFAULT_MAX_AGE_SECONDS,
    cache_ttl_seconds: int = _CACHE_TTL_SECONDS,
    now: float | None = None,
) -> dict[str, int]:
    """清理截图静态目录中的过期文件。

    Args:
        max_age_seconds: 主目录文件最大保留时间（秒），默认 1 小时
        cache_ttl_seconds: cache 子目录文件最大保留时间（秒），默认 600s
        now: 当前时间戳（测试用），默认 time.time()

    Returns:
        dict: {"removed": 删除数, "skipped": 跳过数, "errors": 错误数}
    """
    if now is None:
        now = time.time()

    stats = {"removed": 0, "skipped": 0, "errors": 0}

    if not os.path.isdir(_CAPTURE_STATIC_DIR):
        logger.debug("截图静态目录不存在: %s", _CAPTURE_STATIC_DIR)
        return stats

    # 1. 清理主目录中的过期 PNG 文件（非 cache 子目录）
    for entry in os.listdir(_CAPTURE_STATIC_DIR):
        entry_path = os.path.join(_CAPTURE_STATIC_DIR, entry)
        if not os.path.isfile(entry_path):
            continue  # 跳过子目录（如 cache）
        if not entry.lower().endswith(".png"):
            continue  # 跳过非 PNG 文件

        try:
            mtime = os.path.getmtime(entry_path)
        except OSError as exc:
            logger.warning("获取文件 mtime 失败: %s error=%s", entry_path, exc)
            stats["errors"] += 1
            continue

        if now - mtime > max_age_seconds:
            if _safe_remove(entry_path, reason=f"expired_{int(now - mtime)}s"):
                stats["removed"] += 1
            else:
                stats["errors"] += 1
        else:
            stats["skipped"] += 1

    # 2. 清理 cache 子目录中的过期缓存文件
    cache_dir = os.path.join(_CAPTURE_STATIC_DIR, _CACHE_SUBDIR)
    if os.path.isdir(cache_dir):
        for entry in os.listdir(cache_dir):
            entry_path = os.path.join(cache_dir, entry)
            if not os.path.isfile(entry_path):
                continue
            if not entry.lower().endswith(".png"):
                continue

            try:
                mtime = os.path.getmtime(entry_path)
            except OSError as exc:
                logger.warning("获取缓存文件 mtime 失败: %s error=%s", entry_path, exc)
                stats["errors"] += 1
                continue

            if now - mtime > cache_ttl_seconds:
                if _safe_remove(entry_path, reason=f"cache_expired_{int(now - mtime)}s"):
                    stats["removed"] += 1
                else:
                    stats["errors"] += 1
            else:
                stats["skipped"] += 1

    logger.info(
        "截图清理完成: dir=%s removed=%d skipped=%d errors=%d",
        _CAPTURE_STATIC_DIR,
        stats["removed"],
        stats["skipped"],
        stats["errors"],
    )
    return stats


def cleanup_old_captures(keep_count: int = 10) -> dict[str, int]:
    """保留最近的 N 个截图文件，删除其余。

    用于截图成功后清理，避免目录无限增长。
    仅清理主目录（非 cache 子目录），按 mtime 降序排序。

    Args:
        keep_count: 保留最近 N 个文件

    Returns:
        dict: {"removed": 删除数, "kept": 保留数, "errors": 错误数}
    """
    stats = {"removed": 0, "kept": 0, "errors": 0}

    if not os.path.isdir(_CAPTURE_STATIC_DIR):
        return stats

    # 收集主目录中的 PNG 文件（非 cache 子目录）
    png_files: list[tuple[float, str]] = []
    for entry in os.listdir(_CAPTURE_STATIC_DIR):
        entry_path = os.path.join(_CAPTURE_STATIC_DIR, entry)
        if not os.path.isfile(entry_path):
            continue
        if not entry.lower().endswith(".png"):
            continue
        try:
            mtime = os.path.getmtime(entry_path)
            png_files.append((mtime, entry_path))
        except OSError as exc:
            logger.warning("获取文件 mtime 失败: %s error=%s", entry_path, exc)
            stats["errors"] += 1

    # 按 mtime 降序排序（最新的在前）
    png_files.sort(key=lambda x: x[0], reverse=True)

    # 保留最近 keep_count 个，删除其余
    for i, (_, path) in enumerate(png_files):
        if i < keep_count:
            stats["kept"] += 1
        else:
            if _safe_remove(path, reason=f"old_keep_{keep_count}"):
                stats["removed"] += 1
            else:
                stats["errors"] += 1

    logger.info(
        "截图保留清理完成: dir=%s kept=%d removed=%d errors=%d",
        _CAPTURE_STATIC_DIR,
        stats["kept"],
        stats["removed"],
        stats["errors"],
    )
    return stats


if __name__ == "__main__":
    # 自测：创建临时目录，模拟过期文件清理
    import tempfile

    original_dir = _CAPTURE_STATIC_DIR

    with tempfile.TemporaryDirectory() as tmpdir:
        # 临时替换静态目录
        import app.services.capture_cleanup as cc

        cc._CAPTURE_STATIC_DIR = tmpdir

        # 创建 3 个"新"文件和 2 个"过期"文件
        now = time.time()

        # 新文件（1 小时内）
        for i in range(3):
            path = os.path.join(tmpdir, f"new_{i}.png")
            with open(path, "wb") as f:
                f.write(b"fake_png" * 100)
            os.utime(path, (now - 600, now - 600))  # 10 分钟前

        # 过期文件（超过 1 小时）
        for i in range(2):
            path = os.path.join(tmpdir, f"old_{i}.png")
            with open(path, "wb") as f:
                f.write(b"fake_png" * 100)
            os.utime(path, (now - 7200, now - 7200))  # 2 小时前

        # cache 子目录
        cache_dir = os.path.join(tmpdir, "cache")
        os.makedirs(cache_dir)

        # 新缓存（TTL 内）
        cache_new = os.path.join(cache_dir, "cache_new.png")
        with open(cache_new, "wb") as f:
            f.write(b"fake_png" * 100)
        os.utime(cache_new, (now - 300, now - 300))  # 5 分钟前

        # 过期缓存
        cache_old = os.path.join(cache_dir, "cache_old.png")
        with open(cache_old, "wb") as f:
            f.write(b"fake_png" * 100)
        os.utime(cache_old, (now - 1200, now - 1200))  # 20 分钟前

        # 测试 cleanup_capture_static_dir
        stats = cc.cleanup_capture_static_dir(now=now)
        print(f"cleanup stats: {stats}")
        assert stats["removed"] == 3, f"应删除 3 个过期文件（2 主目录 + 1 cache）: {stats}"
        assert stats["skipped"] == 4, f"应跳过 4 个未过期文件（3 主目录 + 1 cache）: {stats}"
        print("cleanup_capture_static_dir OK")

        # 验证文件状态
        assert os.path.exists(os.path.join(tmpdir, "new_0.png")), "新文件不应被删除"
        assert os.path.exists(os.path.join(tmpdir, "new_1.png")), "新文件不应被删除"
        assert os.path.exists(os.path.join(tmpdir, "new_2.png")), "新文件不应被删除"
        assert not os.path.exists(os.path.join(tmpdir, "old_0.png")), "过期文件应被删除"
        assert not os.path.exists(os.path.join(tmpdir, "old_1.png")), "过期文件应被删除"
        assert os.path.exists(cache_new), "新缓存不应被删除"
        assert not os.path.exists(cache_old), "过期缓存应被删除"
        print("文件状态验证 OK")

        # 测试 cleanup_old_captures
        # 再创建 5 个文件
        for i in range(5):
            path = os.path.join(tmpdir, f"keep_test_{i}.png")
            with open(path, "wb") as f:
                f.write(b"fake_png" * 100)
            os.utime(path, (now - i * 60, now - i * 60))  # 0-4 分钟前

        stats = cc.cleanup_old_captures(keep_count=3)
        print(f"keep stats: {stats}")
        assert stats["kept"] == 3, f"应保留 3 个文件: {stats}"
        assert stats["removed"] == 5, f"应删除 5 个文件（8 总计 - 3 保留）: {stats}"
        print("cleanup_old_captures OK")

    # 恢复原始目录
    cc._CAPTURE_STATIC_DIR = original_dir

    print("ALL TESTS PASSED")
