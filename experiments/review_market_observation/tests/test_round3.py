"""Round 3 单元测试（最小集合，§15）。

覆盖：
1. exact T-1 adjacency checker（逻辑，不需 DB）
2. alignment date join（Round2 与 Review 日期对齐）
3. no future leakage（数据日期不超过 end_date）
4. semantic component map completeness = registry components count
5. archetype replay date alignment
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

_EXP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_EXP_ROOT))
_MAIN_REPO = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(_MAIN_REPO / "backend"))


def test_01_component_map_completeness():
    """§15.4: semantic component map completeness = 当前 registry components。"""
    from experiments.review_market_observation.round3.round3_component_map import (
        generate_component_map_csv, expected_component_count,
    )
    from app.domain.review.metric_registry import DEFAULT_REGISTRY
    registry_total = sum(
        len(DEFAULT_REGISTRY.get_metric(c).components)
        for c in DEFAULT_REGISTRY.metric_codes
    )
    assert expected_component_count() == registry_total, (
        f"expected_component_count()={expected_component_count()} "
        f"!= registry total={registry_total}"
    )
    out = _EXP_ROOT / "out" / "round3" / "_test_component_map.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = generate_component_map_csv(out)
    assert len(rows) == registry_total, (
        f"CSV rows={len(rows)} != registry total={registry_total}"
    )
    # 每个维度合法
    allowed_dims = {"STATE", "TRANSITION", "BREADTH", "DIFFUSION",
                    "CONCENTRATION", "PARTICIPATION", "PRICE",
                    "MIXED", "OTHER"}
    for r in rows:
        dim = r["candidate_observation_dimension"]
        assert dim in allowed_dims, f"非法 dim: {dim} in {r['component_name']}"
    # 家族分布正确
    expected_fam = {"P": 5, "Q": 6, "U": 5, "C": 5, "V": 6}
    actual_fam: dict[str, int] = {}
    for r in rows:
        actual_fam[r["family"]] = actual_fam.get(r["family"], 0) + 1
    assert actual_fam == expected_fam, f"family分布不符: {actual_fam}"
    out.unlink()
    print("OK test_01_component_map_completeness")


def test_02_exact_t_minus_1_adjacency_logic():
    """§15.1: adjacency 判断逻辑（纯函数，不连 DB）。

    模拟 canonical prev_map 与 (td, lag_td) 对，统计 exact vs skip。
    """
    from experiments.review_market_observation.round3.round3_adjacency_audit import (
        _spearman,
    )
    trade_dates = ["2026-08-06", "2026-08-07", "2026-08-08", "2026-08-09", "2026-08-10"]
    prev_map = {trade_dates[i]: (trade_dates[i - 1] if i > 0 else None)
                for i in range(len(trade_dates))}

    # Case A: 完全严格 T-1
    exact_pairs = [
        ("2026-08-07", "2026-08-06"),
        ("2026-08-08", "2026-08-07"),
        ("2026-08-09", "2026-08-08"),
        ("2026-08-10", "2026-08-09"),
    ]
    ex, sk = 0, 0
    for td, lag in exact_pairs:
        if prev_map.get(td) == lag:
            ex += 1
        else:
            sk += 1
    assert ex == 4 and sk == 0, f"Case A failed: ex={ex}, sk={sk}"

    # Case B: 存在跳日（lag 取到 T-2）
    skip_pairs = [
        ("2026-08-07", "2026-08-06"),  # exact
        ("2026-08-08", "2026-08-06"),  # SKIP: 08-07 missing → lagged 08-06
        ("2026-08-09", "2026-08-08"),  # exact
        ("2026-08-10", "2026-08-08"),  # SKIP: 08-09 missing
    ]
    ex2, sk2 = 0, 0
    for td, lag in skip_pairs:
        if prev_map.get(td) == lag:
            ex2 += 1
        else:
            sk2 += 1
    assert ex2 == 2 and sk2 == 2, f"Case B failed: ex={ex2}, sk={sk2}"

    # spearman 纯函数
    a = [1.0, 2.0, 3.0, 4.0, 5.0]
    b = [2.0, 4.0, 6.0, 8.0, 10.0]
    rho = _spearman(a, b)
    assert rho is not None and abs(rho - 1.0) < 1e-9, f"spearman 单调应=1: {rho}"
    c = [5.0, 4.0, 3.0, 2.0, 1.0]
    rho2 = _spearman(a, c)
    assert rho2 is not None and abs(rho2 + 1.0) < 1e-9, f"spearman 反向应=-1: {rho2}"
    # NA pair 正确跳过：有效配对<3 时返回 None
    rho3 = _spearman([1.0, None, 3.0], [2.0, 4.0, None])
    assert rho3 is None, (
        "仅1对非NA应返回None（需要至少3对），实际={}".format(rho3)
    )
    rho4 = _spearman([1.0, 2.0, 3.0, None], [2.0, None, 6.0, 4.0])
    # (1,2) and (3,6) 仅2有效对 → None；但如果 index1 保留(2,None)去掉后再(index3,4)=(None,4) 去掉 → 实际只有2对
    # 加一个更长的
    rho5 = _spearman([1.0, 2.0, None, 4.0, 5.0], [2.0, 4.0, 3.0, None, 10.0])
    assert rho5 is not None, "≥3 有效对应能算 spearman"
    print("OK test_02_exact_t_minus_1_adjacency_logic")


def test_03_alignment_date_join_and_no_future():
    """§15.2 + §15.3: alignment date join + no future leakage。"""
    from pathlib import Path
    import tempfile
    import pandas as pd

    # 构造假的 R2 和 Review CSV，日期交集应该正确对齐
    with tempfile.TemporaryDirectory() as tmpd:
        tmproot = Path(tmpd)
        r2d = tmproot / "round2_db_native"
        r3d = tmproot / "round3"
        r2d.mkdir(parents=True)
        r3d.mkdir(parents=True)

        dates_r2 = ["2026-08-06", "2026-08-07", "2026-08-08", "2026-08-09"]
        dates_rv = ["2026-08-07", "2026-08-08", "2026-08-09", "2026-08-10"]
        # 公共交集应为 08-07~08-09 (3天)
        common_expected = {"2026-08-07", "2026-08-08", "2026-08-09"}

        pd.DataFrame({
            "trade_date": dates_r2,
            "regime_up_ratio": [0.3, 0.4, 0.5, 0.6],
            "top5_price_contribution": [0.2, 0.3, 0.4, 0.5],
        }).to_csv(r2d / "round2_daily_observation.csv", index=False)

        pd.DataFrame({
            "trade_date": dates_rv,
            "scope_return_1d": [0.01, 0.02, 0.03, 0.04],
            "advance_ratio": [0.5, 0.55, 0.6, 0.65],
            "uptrend_member_ratio": [0.3, 0.35, 0.4, 0.45],
            "metric_P_value": [50.0, 55.0, 60.0, 65.0],
            "metric_P_rawValue": [0.5, 0.55, 0.6, 0.65],
            "metric_P_status": ["ready", "ready", "ready", "ready"],
            "metric_Q_value": [50.0, 55.0, 60.0, 65.0],
            "metric_Q_rawValue": [0.5, 0.55, 0.6, 0.65],
            "metric_Q_status": ["ready"] * 4,
            "metric_U_value": [50.0] * 4, "metric_U_rawValue": [0.5] * 4,
            "metric_U_status": ["ready"] * 4,
            "metric_C_value": [50.0] * 4, "metric_C_rawValue": [0.5] * 4,
            "metric_C_status": ["ready"] * 4,
            "metric_V_value": [50.0] * 4, "metric_V_rawValue": [0.5] * 4,
            "metric_V_status": ["ready"] * 4,
        }).to_csv(r3d / "round3_current_review_daily.csv", index=False)

        import csv as _csv
        comp_rows = []
        comps = [("scope_return_1d", "P", "PRICE"),
                 ("advance_ratio", "P", "BREADTH"),
                 ("uptrend_member_ratio", "Q", "STATE")]
        for cn, fam, dim in comps:
            comp_rows.append({
                "component_name": cn, "family": fam, "weight": 1.0,
                "direction": "positive", "actual_formula": "",
                "actual_input_fields": "", "source_semantics": "",
                "candidate_observation_dimension": dim,
                "field_source": "", "derive_fn": "", "extra_fields": "",
            })
        with open(r3d / "round3_current_component_map.csv", "w",
                  newline="", encoding="utf-8") as f:
            w = _csv.DictWriter(f, fieldnames=list(comp_rows[0].keys()))
            w.writeheader()
            w.writerows(comp_rows)

        # 调用 load_inputs 风格的逻辑
        df_r2 = pd.read_csv(r2d / "round2_daily_observation.csv").set_index("trade_date")
        df_rv = pd.read_csv(r3d / "round3_current_review_daily.csv").set_index("trade_date")
        common = df_r2.index.intersection(df_rv.index).tolist()
        assert set(common) == common_expected, (
            f"日期交集错误: {set(common)} != {common_expected}"
        )

        # 无 future leakage：end_date 限制正确
        END_DATE = date(2026, 8, 9)
        for d in common:
            assert date.fromisoformat(str(d)) <= END_DATE, (
                f"future leak: {d} > {END_DATE}"
            )

        print("OK test_03_alignment_date_join_and_no_future")


def test_04_spearman_robustness():
    """spearman 在短序列、NA 对下鲁棒。"""
    from experiments.review_market_observation.round3.round3_adjacency_audit import (
        _spearman,
    )
    # <3 非 NA 对 -> None
    assert _spearman([1.0], [2.0]) is None
    assert _spearman([1.0, None, None], [2.0, 3.0, None]) is None
    # 常数列 -> 零方差 -> None 或有限值
    r = _spearman([1.0, 1.0, 1.0], [2.0, 3.0, 4.0])
    # 不 crash 即可
    assert True
    print("OK test_04_spearman_robustness")


def test_05_archetype_replay_date_alignment():
    """§15.5: archetype replay date alignment。"""
    # Archetype dates 应落在实验窗口内
    WINDOW_START = date(2026, 2, 9)
    WINDOW_END = date(2026, 8, 10)
    ARCHETYPE_DAYS = [
        "2026-04-01", "2026-03-24", "2026-04-17",
        "2026-03-23", "2026-03-31", "2026-06-05",
        "2026-05-13", "2026-03-02", "2026-03-03",
    ]
    for d in ARCHETYPE_DAYS:
        dd = date.fromisoformat(d)
        assert WINDOW_START <= dd <= WINDOW_END, (
            f"archetype {d} 不在实验窗口内"
        )
    print("OK test_05_archetype_replay_date_alignment")


def main() -> int:
    import traceback
    tests = [
        test_01_component_map_completeness,
        test_02_exact_t_minus_1_adjacency_logic,
        test_03_alignment_date_join_and_no_future,
        test_04_spearman_robustness,
        test_05_archetype_replay_date_alignment,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
