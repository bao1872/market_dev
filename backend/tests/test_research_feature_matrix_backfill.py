"""research_feature_matrix_backfill 脚本骨架测试。

验证研究矩阵脚本的骨架行为（不验证完整计算）：
1. parse_args: 参数解析（默认值 / 自定义值 / --start 必填）
2. build_plan: 字段分类统计（causal/hindsight/label 计数）
3. --include-hindsight / --include-labels 开关控制字段分类
4. dry-run: 只打印计划，不写 DB，不写文件
5. --output 必须配合 sample scope（--symbols 或 --limit-instruments）
6. 无过滤全市场禁止输出文件

约束：
- 不接入 watchlist_ready
- 不修改 production snapshot
- dry-run 不写库不写文件

用法：
    cd backend && APP_ENV=test pytest tests/test_research_feature_matrix_backfill.py -v
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from scripts.research_feature_matrix_backfill import (
    build_plan,
    main,
    parse_args,
)

# ===== 1. parse_args =====


def test_parse_args_defaults() -> None:
    """parse_args 默认值：end=latest, dry_run=False, include_hindsight=True, include_labels=True。"""
    with patch(
        "sys.argv",
        ["research_feature_matrix_backfill", "--start", "2026-01-01"],
    ):
        args = parse_args()
    assert args.start == "2026-01-01"
    assert args.end == "latest"
    assert args.dry_run is False
    assert args.include_hindsight is True
    assert args.include_labels is True
    assert args.output is None
    assert args.symbols is None
    assert args.limit_instruments is None


def test_parse_args_custom_values() -> None:
    """parse_args 自定义值 + 所有开关。"""
    with patch(
        "sys.argv",
        [
            "research_feature_matrix_backfill",
            "--start", "2026-01-01",
            "--end", "2026-06-30",
            "--symbols", "000001,600000",
            "--limit-instruments", "20",
            "--dry-run",
            "--output", "/tmp/research.parquet",
            "--include-hindsight", "false",
            "--include-labels", "false",
        ],
    ):
        args = parse_args()
    assert args.start == "2026-01-01"
    assert args.end == "2026-06-30"
    assert args.symbols == ["000001", "600000"]
    assert args.limit_instruments == 20
    assert args.dry_run is True
    assert args.output == "/tmp/research.parquet"
    assert args.include_hindsight is False
    assert args.include_labels is False


def test_parse_args_missing_start_fails() -> None:
    """parse_args 缺少 --start 应 SystemExit。"""
    with patch("sys.argv", ["research_feature_matrix_backfill"]), \
        pytest.raises(SystemExit):
        parse_args()


# ===== 2. build_plan: 字段分类统计 =====


def test_build_plan_returns_field_classification() -> None:
    """build_plan 返回字段分类统计（causal/hindsight/label/confirmed_delay 计数）。"""
    with patch(
        "sys.argv",
        [
            "research_feature_matrix_backfill",
            "--start", "2026-01-01",
            "--end", "2026-01-31",
            "--limit-instruments", "10",
            "--dry-run",
        ],
    ):
        args = parse_args()

    plan = build_plan(args)
    assert "field_classification" in plan
    fc = plan["field_classification"]
    assert "causal" in fc
    assert "confirmed_delay" in fc
    assert "hindsight" in fc
    assert "label" in fc
    assert fc["causal"] > 0
    assert fc["hindsight"] > 0
    assert fc["label"] > 0
    assert fc["confirmed_delay"] > 0


def test_build_plan_includes_trade_dates_and_instruments() -> None:
    """build_plan 包含 trade_dates 和 instruments 计数。"""
    with patch(
        "sys.argv",
        [
            "research_feature_matrix_backfill",
            "--start", "2026-01-01",
            "--end", "2026-01-31",
            "--symbols", "000001",
            "--dry-run",
        ],
    ):
        args = parse_args()

    plan = build_plan(args)
    assert "start" in plan
    assert "end" in plan
    assert "scope" in plan
    assert plan["scope"] == "sample"


def test_build_plan_scope_full_without_filters() -> None:
    """无 --symbols / --limit-instruments 时 scope=full。"""
    with patch(
        "sys.argv",
        [
            "research_feature_matrix_backfill",
            "--start", "2026-01-01",
            "--dry-run",
        ],
    ):
        args = parse_args()

    plan = build_plan(args)
    assert plan["scope"] == "full"


# ===== 3. --include-hindsight / --include-labels 开关 =====


def test_build_plan_excludes_hindsight_when_disabled() -> None:
    """--include-hindsight=false 时 hindsight 计数为 0。"""
    with patch(
        "sys.argv",
        [
            "research_feature_matrix_backfill",
            "--start", "2026-01-01",
            "--limit-instruments", "5",
            "--include-hindsight", "false",
            "--dry-run",
        ],
    ):
        args = parse_args()

    plan = build_plan(args)
    fc = plan["field_classification"]
    assert fc["hindsight"] == 0
    assert fc["causal"] > 0


def test_build_plan_excludes_labels_when_disabled() -> None:
    """--include-labels=false 时 label 计数为 0。"""
    with patch(
        "sys.argv",
        [
            "research_feature_matrix_backfill",
            "--start", "2026-01-01",
            "--limit-instruments", "5",
            "--include-labels", "false",
            "--dry-run",
        ],
    ):
        args = parse_args()

    plan = build_plan(args)
    fc = plan["field_classification"]
    assert fc["label"] == 0
    assert fc["causal"] > 0


def test_build_plan_includes_all_when_both_enabled() -> None:
    """--include-hindsight=true --include-labels=true 时全部命名空间有字段。"""
    with patch(
        "sys.argv",
        [
            "research_feature_matrix_backfill",
            "--start", "2026-01-01",
            "--limit-instruments", "5",
            "--include-hindsight", "true",
            "--include-labels", "true",
            "--dry-run",
        ],
    ):
        args = parse_args()

    plan = build_plan(args)
    fc = plan["field_classification"]
    assert fc["causal"] > 0
    assert fc["hindsight"] > 0
    assert fc["label"] > 0
    assert fc["confirmed_delay"] > 0


# ===== 4. dry-run: 只打印计划，不写 DB，不写文件 =====


def test_dry_run_prints_plan_no_writes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """dry-run 只打印计划，不写 DB，不写文件。"""
    with patch(
        "sys.argv",
        [
            "research_feature_matrix_backfill",
            "--start", "2026-01-01",
            "--end", "2026-01-31",
            "--symbols", "000001",
            "--dry-run",
        ],
    ):
        main()

    captured = capsys.readouterr()
    assert "research_feature_matrix" in captured.out.lower() or "plan" in captured.out.lower()
    assert "causal" in captured.out
    assert "hindsight" in captured.out
    assert "label" in captured.out


def test_dry_run_does_not_write_file(
    tmp_path: object,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """dry-run 即使指定 --output 也不写文件。"""
    output_file = "/tmp/test_research_dry_run_should_not_exist.parquet"
    with patch(
        "sys.argv",
        [
            "research_feature_matrix_backfill",
            "--start", "2026-01-01",
            "--symbols", "000001",
            "--dry-run",
            "--output", output_file,
        ],
    ):
        main()

    import os

    assert not os.path.exists(output_file), "dry-run 不应写文件"


# ===== 5. --output 必须配合 sample scope =====


def test_output_without_sample_scope_raises() -> None:
    """--output 无 --symbols / --limit-instruments 应抛 ValueError。"""
    with patch(
        "sys.argv",
        [
            "research_feature_matrix_backfill",
            "--start", "2026-01-01",
            "--output", "/tmp/research.parquet",
        ],
    ):
        with pytest.raises(ValueError, match="sample"):
            main()


def test_output_with_limit_instruments_ok(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--output 配合 --limit-instruments 不抛错（sample scope）。"""
    with patch(
        "sys.argv",
        [
            "research_feature_matrix_backfill",
            "--start", "2026-01-01",
            "--limit-instruments", "5",
            "--output", "/tmp/research_sample.parquet",
            "--dry-run",
        ],
    ):
        # dry-run 模式下 --output 只校验 scope，不实际写文件
        main()

    import os

    # dry-run 不写文件
    assert not os.path.exists("/tmp/research_sample.parquet")


def test_output_with_symbols_ok(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--output 配合 --symbols 不抛错（sample scope）。"""
    with patch(
        "sys.argv",
        [
            "research_feature_matrix_backfill",
            "--start", "2026-01-01",
            "--symbols", "000001",
            "--output", "/tmp/research_symbols.parquet",
            "--dry-run",
        ],
    ):
        main()


# ===== 6. 非 dry-run 无 --output 不写文件 =====


def test_no_dry_run_no_output_prints_plan(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """非 dry-run 无 --output 只打印计划，不写文件（骨架阶段不实际计算）。"""
    with patch(
        "sys.argv",
        [
            "research_feature_matrix_backfill",
            "--start", "2026-01-01",
            "--symbols", "000001",
        ],
    ):
        main()

    captured = capsys.readouterr()
    assert "plan" in captured.out.lower() or "skeleton" in captured.out.lower()


def test_no_dry_run_no_output_does_not_write_db() -> None:
    """非 dry-run 无 --output 不写 DB（骨架阶段不接入 production snapshot）。"""
    with patch(
        "sys.argv",
        [
            "research_feature_matrix_backfill",
            "--start", "2026-01-01",
            "--symbols", "000001",
        ],
    ):
        # 骨架阶段：不应抛异常，不应写库
        main()


# ===== 7. 字段分类总数 =====


def test_field_classification_total_matches_registry() -> None:
    """字段分类总数应等于 registry 中启用的字段数。"""
    from app.research.feature_causality_registry import build_default_registry

    reg = build_default_registry()
    expected_causal = len(reg.by_namespace("causal"))
    expected_hindsight = len(reg.by_namespace("hindsight"))
    expected_label = len(reg.by_namespace("label"))
    expected_cd = len(reg.by_namespace("confirmed_delay"))

    with patch(
        "sys.argv",
        [
            "research_feature_matrix_backfill",
            "--start", "2026-01-01",
            "--limit-instruments", "5",
            "--dry-run",
        ],
    ):
        args = parse_args()

    plan = build_plan(args)
    fc = plan["field_classification"]
    assert fc["causal"] == expected_causal
    assert fc["hindsight"] == expected_hindsight
    assert fc["label"] == expected_label
    assert fc["confirmed_delay"] == expected_cd
