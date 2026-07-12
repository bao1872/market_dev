"""测试 reporting.py — manifest/输出/50MB 门禁/3 run 保留。"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from app.research.regime_discovery.reporting import (
    MAX_RUNS_KEPT,
    OUTPUT_SIZE_LIMIT_MB,
    REQUIRED_OUTPUT_FILES,
    create_run_dir,
    enforce_max_runs,
    enforce_output_size,
    generate_report_md,
    list_output_files,
    write_all,
    write_manifest,
)


class TestEnforceOutputSize:
    def test_raises_over_50mb(self, tmp_path: Path):
        # 创建一个超过 50MB 的假文件
        big_file = tmp_path / "big.bin"
        # 51 MB
        with open(big_file, "wb") as f:
            f.truncate(51 * 1024 * 1024)
        with pytest.raises(RuntimeError, match="超过"):
            enforce_output_size(tmp_path)

    def test_passes_under_50mb(self, tmp_path: Path):
        small_file = tmp_path / "small.txt"
        small_file.write_text("hello")
        # 不抛异常即通过
        enforce_output_size(tmp_path)

    def test_threshold_value(self):
        assert OUTPUT_SIZE_LIMIT_MB == 50


class TestEnforceMaxRuns:
    def test_keeps_3(self, tmp_path: Path):
        # 创建 5 个 run 目录
        for i in range(5):
            d = tmp_path / f"run_{i}"
            d.mkdir()
            (d / "file.txt").write_text("x")
        enforce_max_runs(tmp_path, keep=3)
        remaining = [d for d in tmp_path.iterdir() if d.is_dir()]
        assert len(remaining) == 3

    def test_no_remove_if_under_keep(self, tmp_path: Path):
        for i in range(2):
            (tmp_path / f"run_{i}").mkdir()
        enforce_max_runs(tmp_path, keep=3)
        remaining = [d for d in tmp_path.iterdir() if d.is_dir()]
        assert len(remaining) == 2

    def test_keep_default(self):
        assert MAX_RUNS_KEPT == 3


class TestWriteManifest:
    def test_contains_required_fields(self, tmp_path: Path):
        path = tmp_path / "manifest.json"
        write_manifest(
            path,
            git_sha="abc123",
            data_as_of="2026-07-08",
            sql_row_count=621769,
            feature_list=["atr_pct", "bb_percent_b"],
            excluded_reasons={"close": "缺失"},
            seed=42,
            thresholds={"silhouette_min": 0.08},
            model_params={"k_range": [3, 8]},
            peak_rss_mb=512.0,
            representation="both",
            sample_rows=150000,
            k_range=(3, 8),
        )
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["git_sha"] == "abc123"
        assert data["data_as_of"] == "2026-07-08"
        assert data["sql_row_count"] == 621769
        assert data["seed"] == 42
        assert data["representation"] == "both"
        assert data["sample_rows"] == 150000
        assert data["k_range"] == [3, 8]
        assert data["feature_list"] == ["atr_pct", "bb_percent_b"]
        assert data["excluded_reasons"] == {"close": "缺失"}
        assert data["peak_rss_mb"] == 512.0


class TestWriteAll:
    def test_creates_8_files(self, tmp_path: Path):
        run_dir = tmp_path / "run_test"
        run_dir.mkdir()
        manifest_data = {
            "run_id": "run_test", "git_sha": "abc", "data_as_of": "2026-07-08",
            "sample_rows": 100, "seed": 42, "representation": "absolute",
            "k_range": [3, 8], "peak_rss_mb": 100.0,
            "feature_list": ["atr_pct"], "excluded_reasons": {},
            "thresholds": {}, "sql_row_count": 100,
        }
        write_all(
            run_dir,
            manifest_data=manifest_data,
            distribution_df=pd.DataFrame([{"feature": "atr_pct", "count": 100}]),
            drift_df=pd.DataFrame([{"feature": "atr_pct", "month": "2026-07", "psi_vs_first": 0.0}]),
            model_sel_df=pd.DataFrame([{"k": 3, "silhouette": 0.1}]),
            profiles_df=pd.DataFrame([{"cluster": "R1", "count": 50, "ratio": 0.5}]),
            stability_df=pd.DataFrame([{"k": 3, "pass": True}]),
            transition_df=pd.DataFrame({"R1": [0.9, 0.1], "R2": [0.1, 0.9]}, index=["R1", "R2"]),
            report_md="# Report\n",
        )
        files = list_output_files(run_dir)
        # 应有 8 个文件
        assert len(files) == 8
        for req in REQUIRED_OUTPUT_FILES:
            assert req in files, f"缺文件 {req}"

    def test_report_md_generated(self, tmp_path: Path):
        manifest = {
            "run_id": "test", "git_sha": "abc", "data_as_of": "2026-07-08",
            "sample_rows": 100, "seed": 42, "representation": "absolute",
            "k_range": [3, 8], "peak_rss_mb": 100.0,
            "feature_list": ["atr_pct"], "excluded_reasons": {},
            "thresholds": {"silhouette_min": 0.08},
        }
        md = generate_report_md(
            manifest=manifest,
            distribution=pd.DataFrame([{"feature": "atr_pct", "count": 100, "null_rate": 0.0, "finite_rate": 1.0}]),
            drift=pd.DataFrame(),
            model_selection=pd.DataFrame([{"k": 3, "silhouette": 0.1}]),
            cluster_profiles=pd.DataFrame(),
            cluster_stability=pd.DataFrame(),
            transition=pd.DataFrame(),
            k_selected=3,
            stable=True,
        )
        assert "# Regime Discovery 报告" in md
        assert "git_sha" in md.lower() or "Git SHA" in md
        assert "atr_pct" in md


class TestCreateRunDir:
    def test_creates_dir(self, tmp_path: Path):
        run_dir = create_run_dir(str(tmp_path), seed=42)
        assert run_dir.exists()
        assert run_dir.is_dir()
        assert "42" in run_dir.name

    def test_creates_base_if_missing(self, tmp_path: Path):
        base = tmp_path / "deep" / "nested" / "path"
        run_dir = create_run_dir(str(base), seed=42)
        assert run_dir.exists()
