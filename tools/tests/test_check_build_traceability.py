"""tools/check_build_traceability.py 纯函数单测（不连接生产 DB / Docker）。

覆盖 P0-4 修正后的纯函数：
1. _sha_matches：候选至少 7 位才参与匹配；full / short / 前缀匹配；
2. classify_worker_heartbeat：非 running 跳过、running+unknown FAIL、running+匹配 PASS、running+不匹配 FAIL；
3. parse_docker_inspect：从 docker inspect JSON 解析 image tag 与多种 OCI revision label；
4. check_docker_image：unknown/<none> FAIL、tag 匹配 PASS、label 匹配 PASS、均不匹配 FAIL。

均为纯函数测试，不发起任何 DB / Docker / 网络调用。

运行:
    python -m pytest tools/tests/test_check_build_traceability.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 将 tools/ 加入 sys.path 以导入 check_build_traceability
_TOOLS_DIR = Path(__file__).resolve().parents[1]
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import check_build_traceability as cbt  # noqa: E402

# 固定 HEAD（40 位）与 short（7 位）
_HEAD_FULL = "f450159abcdef0123456789abcdef0123456789a"
_HEAD_SHORT = "f450159"


# ---------------------------------------------------------------------------
# _sha_matches
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        (None, False),  # 空
        ("", False),  # 空串
        ("f45015", False),  # 6 位 < 7，不足以匹配
        ("f450159", True),  # 正好 7 位 == short
        (_HEAD_FULL, True),  # 完整 SHA
        ("f450159abcdef", True),  # HEAD 前缀（>=7 位）
        ("deadbeef", False),  # 无关 SHA
        ("f450159x", False),  # 前缀不匹配（含非法字符导致 startswith 失败）
    ],
)
def test_sha_matches(candidate, expected):
    assert cbt._sha_matches(candidate, _HEAD_FULL, _HEAD_SHORT) is expected


# ---------------------------------------------------------------------------
# classify_worker_heartbeat
# ---------------------------------------------------------------------------
def test_classify_non_running_is_skipped():
    ok, reason = cbt.classify_worker_heartbeat(
        "after_close", "stopped", "unknown", _HEAD_FULL, _HEAD_SHORT
    )
    assert ok is None
    assert "非 running" in reason


def test_classify_non_running_none_status_is_skipped():
    ok, _ = cbt.classify_worker_heartbeat(
        "after_close", None, None, _HEAD_FULL, _HEAD_SHORT
    )
    assert ok is None


def test_classify_running_unknown_build_sha_fails():
    ok, reason = cbt.classify_worker_heartbeat(
        "after_close", "running", "unknown", _HEAD_FULL, _HEAD_SHORT
    )
    assert ok is False
    assert "未知" in reason


def test_classify_running_none_build_sha_fails():
    ok, _ = cbt.classify_worker_heartbeat(
        "after_close", "running", None, _HEAD_FULL, _HEAD_SHORT
    )
    assert ok is False


def test_classify_running_matching_build_sha_passes():
    ok, reason = cbt.classify_worker_heartbeat(
        "after_close", "running", _HEAD_SHORT, _HEAD_FULL, _HEAD_SHORT
    )
    assert ok is True
    assert "一致" in reason


def test_classify_running_full_sha_passes():
    ok, _ = cbt.classify_worker_heartbeat(
        "after_close", "running", _HEAD_FULL, _HEAD_FULL, _HEAD_SHORT
    )
    assert ok is True


def test_classify_running_mismatching_build_sha_fails():
    ok, reason = cbt.classify_worker_heartbeat(
        "after_close", "running", "deadbeefcafe", _HEAD_FULL, _HEAD_SHORT
    )
    assert ok is False
    assert "不一致" in reason


# ---------------------------------------------------------------------------
# parse_docker_inspect
# ---------------------------------------------------------------------------
def test_parse_docker_inspect_tag_and_oci_revision():
    cj = {
        "Config": {
            "Image": "trading-worker-after-close:f450159",
            "Labels": {"org.opencontainers.image.revision": _HEAD_FULL},
        }
    }
    image_tag, revision_label = cbt.parse_docker_inspect(cj)
    assert image_tag == "trading-worker-after-close:f450159"
    assert revision_label == _HEAD_FULL


@pytest.mark.parametrize(
    "label_key",
    [
        "org.opencontainers.image.revision",
        "org.label-schema.vcs-ref",
        "vcs-ref",
        "revision",
    ],
)
def test_parse_docker_inspect_alternate_label_keys(label_key):
    cj = {"Config": {"Image": "img:tag", "Labels": {label_key: _HEAD_SHORT}}}
    _, revision_label = cbt.parse_docker_inspect(cj)
    assert revision_label == _HEAD_SHORT


def test_parse_docker_inspect_missing_config_and_labels():
    image_tag, revision_label = cbt.parse_docker_inspect({})
    assert image_tag is None
    assert revision_label is None


def test_parse_docker_inspect_null_labels():
    cj = {"Config": {"Image": "img:tag", "Labels": None}}
    image_tag, revision_label = cbt.parse_docker_inspect(cj)
    assert image_tag == "img:tag"
    assert revision_label is None


# ---------------------------------------------------------------------------
# check_docker_image
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "image_tag",
    [None, "", "unknown", "trading-worker-after-close:unknown", "<none>:<none>"],
)
def test_check_docker_image_unknown_or_none_fails(image_tag):
    ok, _ = cbt.check_docker_image(image_tag, None, _HEAD_FULL, _HEAD_SHORT)
    assert ok is False


def test_check_docker_image_tag_segment_matches():
    # name:sha 形式，提取最后一段 tag 与 HEAD short 匹配。
    ok, reason = cbt.check_docker_image(
        f"trading-worker-after-close:{_HEAD_SHORT}", None, _HEAD_FULL, _HEAD_SHORT
    )
    assert ok is True
    assert "一致" in reason


def test_check_docker_image_registry_port_tag_segment_matches():
    # registry:5000/name:sha 形式，冒号属于端口，只取最后一段 tag。
    ok, _ = cbt.check_docker_image(
        f"registry:5000/trading-worker:{_HEAD_FULL}", None, _HEAD_FULL, _HEAD_SHORT
    )
    assert ok is True


def test_check_docker_image_digest_segment_matches():
    # name@sha256:<hex> digest 形式，取 digest 段。
    ok, _ = cbt.check_docker_image(
        f"trading-worker@sha256:{_HEAD_FULL}", None, _HEAD_FULL, _HEAD_SHORT
    )
    assert ok is True


def test_check_docker_image_tag_is_pure_sha_matches():
    # 当镜像 tag 本身就是 SHA（无前缀名）时整串匹配。
    ok, reason = cbt.check_docker_image(_HEAD_FULL, None, _HEAD_FULL, _HEAD_SHORT)
    assert ok is True
    assert "一致" in reason


def test_check_docker_image_label_matches():
    ok, reason = cbt.check_docker_image(
        "trading-worker-after-close:latest", _HEAD_FULL, _HEAD_FULL, _HEAD_SHORT
    )
    assert ok is True
    assert "OCI revision" in reason


def test_check_docker_image_both_mismatch_fails():
    ok, reason = cbt.check_docker_image(
        "trading-worker-after-close:latest", "deadbeefcafe", _HEAD_FULL, _HEAD_SHORT
    )
    assert ok is False
    assert "无法匹配" in reason
