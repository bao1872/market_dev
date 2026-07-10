"""tools/check_build_traceability.py 纯函数单测（不连接生产 DB / Docker）。

覆盖 P0-4 修正后的纯函数：
1. _sha_matches：候选至少 7 位才参与匹配；full / short / 前缀匹配；
2. classify_worker_heartbeat：非 running 跳过、running+unknown FAIL、running+匹配 PASS、running+不匹配 FAIL；
3. parse_docker_inspect：从 docker inspect JSON 解析 image tag 与多种 OCI revision label；
4. check_docker_image：unknown/<none> FAIL、tag 匹配 PASS、label 匹配 PASS、均不匹配 FAIL；
5. parse_container_env_value：从 Config.Env 解析指定环境变量（WORKER_TYPE / GIT_SHA）；
6. check_container_env_git_sha：None 不阻断（None）、unknown/不一致 FAIL、一致 PASS；
7. check_docker_images（注入 containers）：容器 env GIT_SHA 不一致 / 镜像不一致 FAIL、均一致 PASS、未注入 env 通过附提示；
8. check_worker_type_coverage（注入 containers + heartbeat_rows）：缺心跳 FAIL、全覆盖 PASS、无 WORKER_TYPE SKIP；
9. scan_running_containers（monkeypatch _run）：docker 不可用返回 (None, err)、无容器 []、正常解析 name/worker_type/env_git_sha。

均为纯函数测试，不发起任何 DB / Docker / 网络调用。

运行:
    python -m pytest tools/tests/test_check_build_traceability.py -q
"""

from __future__ import annotations

import json
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


# ---------------------------------------------------------------------------
# parse_container_env_value
# ---------------------------------------------------------------------------
def test_parse_container_env_value_found():
    cj = {"Config": {"Env": ["PATH=/usr/bin", "WORKER_TYPE=after_close", "GIT_SHA=" + _HEAD_FULL]}}
    assert cbt.parse_container_env_value(cj, "WORKER_TYPE") == "after_close"
    assert cbt.parse_container_env_value(cj, "GIT_SHA") == _HEAD_FULL


def test_parse_container_env_value_missing_returns_none():
    cj = {"Config": {"Env": ["PATH=/usr/bin"]}}
    assert cbt.parse_container_env_value(cj, "WORKER_TYPE") is None


def test_parse_container_env_value_empty_value_returns_none():
    cj = {"Config": {"Env": ["GIT_SHA="]}}
    assert cbt.parse_container_env_value(cj, "GIT_SHA") is None


def test_parse_container_env_value_no_env_returns_none():
    assert cbt.parse_container_env_value({}, "GIT_SHA") is None
    assert cbt.parse_container_env_value({"Config": {"Env": None}}, "GIT_SHA") is None


def test_parse_container_env_value_ignores_non_string_entries():
    cj = {"Config": {"Env": [123, "GIT_SHA=" + _HEAD_SHORT]}}
    assert cbt.parse_container_env_value(cj, "GIT_SHA") == _HEAD_SHORT


# ---------------------------------------------------------------------------
# check_container_env_git_sha
# ---------------------------------------------------------------------------
def test_check_container_env_git_sha_none_not_blocking():
    ok, reason = cbt.check_container_env_git_sha(None, _HEAD_FULL, _HEAD_SHORT)
    assert ok is None
    assert "不阻断" in reason


def test_check_container_env_git_sha_matches():
    ok, _ = cbt.check_container_env_git_sha(_HEAD_FULL, _HEAD_FULL, _HEAD_SHORT)
    assert ok is True


def test_check_container_env_git_sha_unknown_fails():
    # "unknown" 长度 >=7 但不匹配 HEAD -> FAIL（非 None）
    ok, _ = cbt.check_container_env_git_sha("unknown", _HEAD_FULL, _HEAD_SHORT)
    assert ok is False


def test_check_container_env_git_sha_mismatch_fails():
    ok, reason = cbt.check_container_env_git_sha("deadbeefcafe", _HEAD_FULL, _HEAD_SHORT)
    assert ok is False
    assert "不一致" in reason


# ---------------------------------------------------------------------------
# check_docker_images（注入 containers，不连 docker）
# ---------------------------------------------------------------------------
def _container(**kwargs):
    base = {
        "name": "trading-worker-after-close",
        "worker_type": "after_close",
        "image_tag": f"trading-worker-after-close:{_HEAD_SHORT}",
        "revision_label": _HEAD_FULL,
        "env_git_sha": _HEAD_FULL,
        "inspect_error": False,
    }
    base.update(kwargs)
    return base


def test_check_docker_images_all_consistent_passes():
    ok, _ = cbt.check_docker_images(
        _HEAD_FULL, _HEAD_SHORT, allow_skip=False, containers=[_container()]
    )
    assert ok is True


def test_check_docker_images_env_git_sha_mismatch_fails():
    c = _container(env_git_sha="deadbeefcafe")
    ok, reason = cbt.check_docker_images(
        _HEAD_FULL, _HEAD_SHORT, allow_skip=False, containers=[c]
    )
    assert ok is False
    assert "GIT_SHA 不一致" in reason


def test_check_docker_images_image_mismatch_fails():
    c = _container(image_tag="trading-worker-after-close:latest", revision_label="deadbeefcafe")
    ok, _ = cbt.check_docker_images(
        _HEAD_FULL, _HEAD_SHORT, allow_skip=False, containers=[c]
    )
    assert ok is False


def test_check_docker_images_env_not_injected_passes():
    # env GIT_SHA 未注入（None）不阻断：镜像一致即整体通过。
    c = _container(env_git_sha=None)
    ok, _ = cbt.check_docker_images(
        _HEAD_FULL, _HEAD_SHORT, allow_skip=False, containers=[c]
    )
    assert ok is True


def test_check_docker_images_inspect_error_fails():
    c = _container(inspect_error=True)
    ok, _ = cbt.check_docker_images(
        _HEAD_FULL, _HEAD_SHORT, allow_skip=False, containers=[c]
    )
    assert ok is False


def test_check_docker_images_empty_containers_skips():
    ok, reason = cbt.check_docker_images(
        _HEAD_FULL, _HEAD_SHORT, allow_skip=False, containers=[]
    )
    assert ok is None
    assert "跳过" in reason


# ---------------------------------------------------------------------------
# check_worker_type_coverage（注入 containers + heartbeat_rows，不连 docker/DB）
# ---------------------------------------------------------------------------
def test_worker_type_coverage_all_present_passes():
    containers = [_container(worker_type="after_close"), _container(name="cap", worker_type="capture")]
    rows = [{"worker_name": "after_close"}, {"worker_name": "capture"}]
    ok, _ = cbt.check_worker_type_coverage(
        _HEAD_FULL, _HEAD_SHORT, allow_skip=False, containers=containers, heartbeat_rows=rows
    )
    assert ok is True


def test_worker_type_coverage_missing_heartbeat_fails():
    containers = [_container(worker_type="after_close"), _container(name="cap", worker_type="capture")]
    rows = [{"worker_name": "after_close"}]
    ok, reason = cbt.check_worker_type_coverage(
        _HEAD_FULL, _HEAD_SHORT, allow_skip=False, containers=containers, heartbeat_rows=rows
    )
    assert ok is False
    assert "capture" in reason


def test_worker_type_coverage_no_worker_type_skips():
    containers = [_container(worker_type=None)]
    ok, _ = cbt.check_worker_type_coverage(
        _HEAD_FULL, _HEAD_SHORT, allow_skip=False, containers=containers, heartbeat_rows=[]
    )
    assert ok is None


# ---------------------------------------------------------------------------
# scan_running_containers（monkeypatch _run，不连 docker）
# ---------------------------------------------------------------------------
class _FakeProc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def test_scan_running_containers_docker_unavailable(monkeypatch):
    monkeypatch.setattr(cbt, "_run", lambda cmd: None)
    containers, err = cbt.scan_running_containers(allow_skip=True)
    assert containers is None
    assert err is not None


def test_scan_running_containers_docker_nonzero(monkeypatch):
    monkeypatch.setattr(cbt, "_run", lambda cmd: _FakeProc(returncode=1, stdout=""))
    containers, err = cbt.scan_running_containers(allow_skip=True)
    assert containers is None
    assert err is not None


def test_scan_running_containers_empty(monkeypatch):
    monkeypatch.setattr(cbt, "_run", lambda cmd: _FakeProc(returncode=0, stdout="   "))
    containers, err = cbt.scan_running_containers(allow_skip=True)
    assert containers == []
    assert err is None


def test_scan_running_containers_parses_relevant(monkeypatch):
    inspect_json = json.dumps({
        "Config": {
            "Image": f"trading-worker-after-close:{_HEAD_SHORT}",
            "Labels": {"org.opencontainers.image.revision": _HEAD_FULL},
            "Env": ["WORKER_TYPE=after_close", "GIT_SHA=" + _HEAD_FULL],
        }
    })

    def fake_run(cmd):
        if cmd[:2] == ["docker", "ps"]:
            # 一个相关容器 + 一个无关容器（应被关键字过滤掉）
            return _FakeProc(stdout="trading-worker-after-close\tcid1\nredis\tcid2\n")
        if cmd[:2] == ["docker", "inspect"]:
            return _FakeProc(stdout=inspect_json)
        return None

    monkeypatch.setattr(cbt, "_run", fake_run)
    containers, err = cbt.scan_running_containers(allow_skip=True)
    assert err is None
    assert len(containers) == 1
    c = containers[0]
    assert c["name"] == "trading-worker-after-close"
    assert c["worker_type"] == "after_close"
    assert c["env_git_sha"] == _HEAD_FULL
    assert c["revision_label"] == _HEAD_FULL
    assert c["inspect_error"] is False


def test_scan_running_containers_inspect_failure_marks_error(monkeypatch):
    def fake_run(cmd):
        if cmd[:2] == ["docker", "ps"]:
            return _FakeProc(stdout="trading-backend\tcid1\n")
        if cmd[:2] == ["docker", "inspect"]:
            return _FakeProc(returncode=1, stdout="")
        return None

    monkeypatch.setattr(cbt, "_run", fake_run)
    containers, err = cbt.scan_running_containers(allow_skip=True)
    assert err is None
    assert len(containers) == 1
    assert containers[0]["inspect_error"] is True
