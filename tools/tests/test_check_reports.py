"""tools/check_reports.py 秘密检测与 SHA 校验测试。

覆盖：
- 秘密赋值检测（password=abc / token=abc / secret=abc / DATABASE_URL=...）
- PEM 私钥标记检测
- 占位值白名单（<redacted> / REDACTED / *** / example / placeholder）
- 说明性文字不误报（"禁止保存 password=" / "检查 token=" / "PRIVATE KEY 属于禁止内容"）
- SHA 字段提取
- is_placeholder / is_empty_or_placeholder 辅助函数

运行:
    python -m pytest tools/tests/test_check_reports.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 将 tools/ 加入 sys.path 以导入 check_reports
_TOOLS_DIR = Path(__file__).resolve().parents[1]
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

# ruff: isort: off
import check_reports as cr  # noqa: E402
# ruff: isort: on


# ──────────────────────────────────────────────────────────────
# 秘密检测：真实赋值必须 FAIL
# ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "line",
    [
        "password=abc",
        "password=abc123",
        "PASSWORD=abc123",
        "token=abc",
        "token=Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIi",
        "TOKEN=abc",
        "secret=abc",
        "secret=sk-xxx123456",
        "SECRET=abc",
        "DATABASE_URL=postgresql://user:pass@host:5432/db",
        "DATABASE_URL=postgresql+psycopg://bz:bz@localhost:5432/bz_stock_test",
        "database_url=postgres://user:pass@host/db",
        "password=\"abc123\"",
        "token=`bearer xxx`",
        "secret='sk-12345'",
    ],
)
def test_real_secret_assignment_fails(line: str) -> None:
    msg = cr.check_line_for_secret(line)
    assert msg is not None, f"应判定为秘密，但未触发: {line!r}"
    assert "秘密" in msg or "PRIVATE KEY" in msg


# ──────────────────────────────────────────────────────────────
# PEM 私钥标记：无条件 FAIL
# ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "line",
    [
        "-----BEGIN RSA PRIVATE KEY-----",
        "-----BEGIN EC PRIVATE KEY-----",
        "-----BEGIN PRIVATE KEY-----",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "some text -----BEGIN PRIVATE KEY----- some text",
    ],
)
def test_pem_private_key_marker_fails(line: str) -> None:
    msg = cr.check_line_for_secret(line)
    assert msg is not None, f"PEM 私钥标记应 FAIL: {line!r}"
    assert "PEM PRIVATE KEY" in msg


# ──────────────────────────────────────────────────────────────
# 占位值：PASS
# ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "line",
    [
        "secret=<redacted>",
        "secret=REDACTED",
        "secret=redacted",
        "secret=***",
        "secret=example",
        "secret=placeholder",
        "password=<redacted>",
        "token=<redacted>",
        "token=`<redacted>`",
        'secret="<redacted>"',
        "DATABASE_URL=<redacted>",
        "secret=xxx",
        "secret=xxxx",
        "secret=<value>",
        "secret=<secret>",
        "secret=<token>",
        "secret=<password>",
        "secret=n/a",
        "secret=na",
    ],
)
def test_placeholder_assignment_passes(line: str) -> None:
    msg = cr.check_line_for_secret(line)
    assert msg is None, f"占位值不应触发秘密告警: {line!r} -> {msg}"


# ──────────────────────────────────────────────────────────────
# 说明性文字：PASS（无 = 后赋值或为占位）
# ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "line",
    [
        # 无值赋值 — 不视为秘密
        "禁止保存 password=",
        "检查 token=",
        "不得包含 secret=",
        "- 不泄露秘密（Token、SSH 私钥、数据库连接、密码）",
        # 含占位值的说明
        "示例 secret=<redacted>",
        "例如 password=***",
        "如 token=example",
        # 仅描述 PRIVATE KEY（无 PEM 标记）
        "PRIVATE KEY 属于禁止内容",
        "不允许保存 PRIVATE KEY",
        "SSH 私钥、PRIVATE KEY 等禁止内容",
        # 普通中文说明
        "本目录不保存密码、Token、SSH 私钥、数据库凭据。",
        "检查 token= 字段是否存在占位",
        "禁止保存 password= 等真实赋值",
        # 不含 = 的引用
        "见 password 字段",
        "Push Result 字段非空",
        # 表头/分隔
        "| Command or Check | Result | Exit Code | Notes |",
        "|---|---|---|---|",
        # Markdown 链接
        "- 详细规则见 `reports/README.md`。",
        # 不匹配的 key
        "user=admin",
        "host=localhost",
        "port=5432",
    ],
)
def test_descriptive_text_passes(line: str) -> None:
    msg = cr.check_line_for_secret(line)
    assert msg is None, f"说明性文字不应触发秘密告警: {line!r} -> {msg}"


# ──────────────────────────────────────────────────────────────
# 辅助函数
# ──────────────────────────────────────────────────────────────


def test_is_placeholder_recognizes_known_values() -> None:
    assert cr.is_placeholder("<redacted>")
    assert cr.is_placeholder("REDACTED")
    assert cr.is_placeholder("***")
    assert cr.is_placeholder("example")
    assert cr.is_placeholder("placeholder")
    assert cr.is_placeholder("`<redacted>`")
    assert cr.is_placeholder("\"<redacted>\"")
    assert cr.is_placeholder("<redacted>,")
    assert not cr.is_placeholder("abc")
    assert not cr.is_placeholder("real-secret-value")
    assert not cr.is_placeholder("postgresql://user:pass@host")


def test_is_placeholder_treats_bare_punctuation_as_placeholder() -> None:
    """值为单纯引号/反引号/标点时（如 secret=` 后跟中文）视为占位，不误报。"""
    assert cr.is_placeholder('"')
    assert cr.is_placeholder("`")
    assert cr.is_placeholder("'")
    assert cr.is_placeholder("`\"")
    assert cr.is_placeholder("")


def test_is_empty_or_placeholder() -> None:
    assert cr.is_empty_or_placeholder("")
    assert cr.is_empty_or_placeholder("（提交后填写）")
    assert cr.is_empty_or_placeholder("(TBD)")
    assert cr.is_empty_or_placeholder("待填写")
    assert cr.is_empty_or_placeholder("tbd")
    assert cr.is_empty_or_placeholder("todo")
    assert cr.is_empty_or_placeholder("pending")
    assert not cr.is_empty_or_placeholder("012681fea1966dc81385822da57e58ae645d88c4")
    assert not cr.is_empty_or_placeholder("SUCCESS")


# ──────────────────────────────────────────────────────────────
# SHA 字段提取
# ──────────────────────────────────────────────────────────────


def test_extract_sha_field_basic() -> None:
    text = """- Base SHA: abc123
- Implementation SHA: def456
- Report Published Through SHA: ghi789
"""
    assert cr.extract_sha_field(text, "Base SHA") == "abc123"
    assert cr.extract_sha_field(text, "Implementation SHA") == "def456"
    assert cr.extract_sha_field(text, "Report Published Through SHA") == "ghi789"


def test_extract_sha_field_missing() -> None:
    text = "- Base SHA: abc123\n"
    assert cr.extract_sha_field(text, "Base SHA") == "abc123"
    assert cr.extract_sha_field(text, "Implementation SHA") is None


def test_extract_sha_field_with_trailing_content() -> None:
    text = "- Implementation SHA: 012681fea1966dc81385822da57e58ae645d88c4（implementation）"
    val = cr.extract_sha_field(text, "Implementation SHA")
    assert val is not None
    assert "012681fea1966dc81385822da57e58ae645d88c4" in val


# ──────────────────────────────────────────────────────────────
# SHA 格式校验
# ──────────────────────────────────────────────────────────────


def test_sha_hex_regex_matches_40char_lowercase() -> None:
    assert cr.SHA_HEX_RE.match("012681fea1966dc81385822da57e58ae645d88c4")
    assert cr.SHA_HEX_RE.match("abcdef0123456789abcdef0123456789abcdef01")


def test_sha_hex_regex_rejects_invalid() -> None:
    assert not cr.SHA_HEX_RE.match("abc123")
    assert not cr.SHA_HEX_RE.match("Z2681fea1966dc81385822da57e58ae645d88c4")  # 含 Z
    assert not cr.SHA_HEX_RE.match("012681FEA1966DC81385822DA57E58AE645D88C4")  # 大写
    assert not cr.SHA_HEX_RE.match("")


# ──────────────────────────────────────────────────────────────
# 端到端：完整报告文本检查
# ──────────────────────────────────────────────────────────────


def test_full_report_with_secret_triggers_violation() -> None:
    """含真实 DATABASE_URL 赋值的报告应被检测。"""
    bad_lines = [
        "## 0. Report Metadata",
        "- Status: COMPLETED",
        "DATABASE_URL=postgresql://user:pass@host:5432/db",
    ]
    violations_found = 0
    for line in bad_lines:
        if cr.check_line_for_secret(line) is not None:
            violations_found += 1
    assert violations_found >= 1


def test_full_report_with_only_descriptive_text_passes() -> None:
    """仅含说明性文字（如 '不保存 password='）的报告不应触发。"""
    lines = [
        "## 0. Report Metadata",
        "- Status: COMPLETED",
        "- 不泄露秘密（Token、SSH 私钥、数据库连接、密码）",
        "reports/ 不保存：密码、Token、SSH 私钥、数据库凭据。",
        "示例 secret=<redacted> 不视为真实秘密。",
        "PRIVATE KEY 属于禁止内容，不应在报告中出现。",
    ]
    for line in lines:
        assert cr.check_line_for_secret(line) is None, f"误报: {line!r}"
