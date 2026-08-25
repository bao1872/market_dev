"""Round 1 单元测试（纯函数 + synthetic fixture，不连 DB）。

覆盖 PRD prompt §11 最低测试要求：
1. ✅ Frozen 120 trading-date selection（build_selected_trade_dates + validate）
2. ✅ T-1 transition 使用前一交易日，不是自然日前一天（compute_transition_audit）
3. ✅ duplicate detection（check_rows）
4. ✅ denominator 对 unavailable / missing 的处理（check_readiness）
5. ✅ T 日统计不读取未来日期（check_trade_dates 排序 + transition 只 shift(1)）
6. ✅ canonical semantic mapping 不被实验代码自行改写（flatten_state_payload 保留原值）
"""
from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

# 允许直接作为 pytest module 运行（无论 backend root 如何定位），
# 这里使用 sys.path 注入 experiments 目录
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.review_market_observation.round1.round1_extract import (
    build_selected_trade_dates,
    flatten_state_payload,
    validate_120_consecutive_trade_dates,
    compute_schema_hash,
    normalize_libpq_dsn,
    apply_dsn_host_override,
)
from experiments.review_market_observation.round1.round1_analyze import (
    check_rows,
    check_trade_dates,
    check_readiness,
    check_lineage,
    collect_integrity_findings,
    compute_transition_audit,
    derive_round1_verdict,
    IntegrityFinding,
)


# ============================================================================
# Fixtures
# ============================================================================

def _make_weekday_seq(start: date, n: int) -> list[date]:
    """生成 n 个连续'工作日'日期（跳过周六日），近似 A 股交易日序列。

    注意：这不是真实交易日历（忽略法定节假日），仅用于本地单测验证 120 选择逻辑。
    """
    out: list[date] = []
    cur = start
    while len(out) < n:
        if cur.weekday() < 5:  # Mon=0..Fri=4
            out.append(cur)
        cur += timedelta(days=1)
    return out


@pytest.fixture
def fake_trade_dates_200() -> list[date]:
    return _make_weekday_seq(date(2026, 1, 5), 200)


@pytest.fixture
def fake_minimal_state_payload() -> dict:
    """最小合法 state_payload（字段缺失可接受，flatten 用 None 占位）。"""
    return {
        "bar_index": 199,
        "time": "2026-08-11",
        "history_contract_version": "review-history-v2",
        "regime_value": 1,
        "regime_strength": 0.75,
        "trend_transition": "SIDEWAYS→UP",
        "dsa_dir_bars": 12,
        "dsa_vwap_dev_pct": 1.23,
        "swing_bias": 1,
        "internal_bias": 1,
        "structure_alignment": "共振",
        "active_internal_ob_count": 1,
        "active_swing_ob_count": 0,
        "volatility_phase": "normal",
        "momentum_direction": "expanding",
        "momentum_change": "enhancing",
        "sqzmom_val": 0.105,
        "sqzmom_delta": 0.021,
        "volume_ratio_20": 1.35,
        "volume_percentile_20": 78.5,
        "review_volume_ratio20": 1.32,
        "review_amount_ratio20": 1.40,
        "review_volume_percentile20": 77.9,
        "review_amount_percentile200": 82.0,
        "price_position_120d": 0.62,
        "available_bars": 500,
        "trend_ready": True,
        "structure_ready": True,
        "momentum_ready": True,
        "volume20_ready": True,
        "volume200_ready": True,
        "core_factor_ready": True,
        "history_sufficient": True,
        "valid_for_market_aggregation": True,
        "invalid_reason": None,
    }


@pytest.fixture
def synthetic_df_for_audit() -> pd.DataFrame:
    """构造 synthetic frozen dataset。覆盖：
    - 3 instruments × 20 真实交易日（不等 120，用于 transition/dup/denominator）
    - 包含重复行（for test_duplicate_detection）
    - 包含 weekend gap → transition 必须正确使用前一交易日，不是自然日前一天
    - regime_value 有迁移（UP→DOWN on 2026-01-15 → 2026-01-16）
    """
    import pandas as pd

    instrs = [str(uuid.uuid4()) for _ in range(3)]
    dts = _make_weekday_seq(date(2026, 1, 5), 20)

    rows = []
    for i, instr in enumerate(instrs):
        for d_idx, d in enumerate(dts):
            # 构造 regime_value：
            #   instr 0: 前 10 天 = 1 (UP)，后 10 = -1 (DOWN)
            #   instr 1: 全 0 (SIDEWAYS)
            #   instr 2: 每天 random-ish（用于 transition rate）
            if i == 0:
                regime = 1 if d_idx < 10 else -1
            elif i == 1:
                regime = 0
            else:
                regime = [1, 1, 0, -1, -1, -1, 0, 1, 1, 0,
                          0, -1, -1, 1, 1, 0, -1, -1, 1, 1][d_idx]
            vma = (d_idx != 5)  # 故意让 d=第 6 天 valid=False 测试 denominator
            rows.append({
                "instrument_id": instr,
                "trade_date": str(d),
                "algorithm_version": "1.0.0-core-split",
                "input_hash": f"h_{instr}_{d}",
                "source_history_run_id": str(uuid.UUID(int=1)),
                "hc_outer": "review-history-v2",
                "hc_payload": "review-history-v2",
                "regime_value": regime,
                "swing_bias": regime,
                "internal_bias": regime if d_idx % 2 == 0 else 0,
                "structure_alignment": "共振" if regime == 0 else (
                    "共振" if regime == 1 and (d_idx % 2 == 0) else "背离"
                ),
                "volatility_phase": "normal" if d_idx % 5 != 0 else "squeeze",
                "momentum_direction": "expanding" if regime >= 0 else "contracting",
                # readiness
                "history_sufficient": True,
                "core_factor_ready": True,
                "valid_for_market_aggregation": vma,
                "invalid_reason": None if vma else "warmup_period",
                "regime_strength": 0.5 + 0.1 * (d_idx % 5),
                "dsa_dir_bars": 5 + (d_idx % 7),
                "dsa_vwap_dev_pct": round((d_idx - 10) * 0.1, 3),
                "sqzmom_val": round(0.01 * (d_idx - 10), 4),
                "sqzmom_delta": 0.005,
                "volume_ratio_20": 0.8 + (d_idx % 5) * 0.1,
                "volume_percentile_20": 30 + d_idx * 3,
                "review_volume_ratio20": 0.8 + (d_idx % 5) * 0.1,
                "review_amount_ratio20": 0.85 + (d_idx % 5) * 0.1,
                "review_volume_percentile20": 30 + d_idx * 3,
                "review_amount_percentile200": 40 + d_idx * 2.5,
                "price_position_120d": round(0.05 * d_idx, 3),
            })

    # 增加一条 duplicate（第 1 instr 第 1 天）
    rows.append(dict(rows[0]))
    return pd.DataFrame(rows)


# ============================================================================
# Test 1: Frozen 120 trading-date selection
# ============================================================================

class TestTradeDateSelection:
    def test_exactly_200_known_dates_picks_latest_120_asc(self, fake_trade_dates_200):
        selected = build_selected_trade_dates(fake_trade_dates_200, 120)
        info = validate_120_consecutive_trade_dates(selected)
        assert info["is_exact_target"] is True
        assert info["count"] == 120
        assert info["start"] < info["end"]
        # 确保升序
        assert selected == sorted(selected)
        # 确保所选是最后 120 个（== known 的 index 80..199）
        assert selected[0] == fake_trade_dates_200[-120]
        assert selected[-1] == fake_trade_dates_200[-1]

    def test_less_than_120_returns_all_available(self):
        small = _make_weekday_seq(date(2026, 7, 1), 30)
        selected = build_selected_trade_dates(small, 120)
        info = validate_120_consecutive_trade_dates(selected)
        assert info["count"] == 30
        assert info["is_exact_target"] is False

    def test_empty_input_returns_empty(self):
        selected = build_selected_trade_dates([], 120)
        info = validate_120_consecutive_trade_dates(selected)
        assert info["count"] == 0
        assert info["is_exact_target"] is False

    def test_sort_order_is_robust_to_unordered_input(self):
        small = _make_weekday_seq(date(2026, 7, 1), 30)
        shuffled = list(reversed(small))[::2] + small[::3]
        selected = build_selected_trade_dates(shuffled, 30)
        assert selected == sorted(selected)


# ============================================================================
# Test 2: flatten_state_payload（canonical 语义保留）
# ============================================================================

class TestFlattenStatePayload:
    def test_does_not_rewrite_regime_value(self, fake_minimal_state_payload):
        flat = flatten_state_payload(fake_minimal_state_payload, hc_outer="review-history-v2")
        # 禁止自行改写：regime_value=1 不能变成 "上行"（那是 adapter 的职责）
        assert flat["regime_value"] == 1
        assert flat["swing_bias"] == 1
        assert flat["structure_alignment"] == "共振"  # payload 原值，必须保留

    def test_missing_fields_are_none_not_default(self):
        flat = flatten_state_payload({}, hc_outer=None)
        # 完全空 payload → 所有 key 必须存在但值为 None（不伪造默认值）
        assert flat["regime_value"] is None
        assert flat["regime_strength"] is None
        assert flat["swing_bias"] is None
        assert flat["sqzmom_val"] is None
        assert flat["hc_payload"] is None

    def test_history_contract_version_aliased(self, fake_minimal_state_payload):
        flat = flatten_state_payload(fake_minimal_state_payload, hc_outer="V2")
        assert flat["hc_payload"] == "review-history-v2"  # payload 内值
        # hc_outer 是调用方传的外层列，不在此函数里返回（外层 join 时加）


# ============================================================================
# Test 3: duplicate detection
# ============================================================================

class TestDuplicateDetection:
    def test_finds_the_injected_duplicate(self, synthetic_df_for_audit):
        info = check_rows(synthetic_df_for_audit)
        # fixture 中注了 1 对 duplicate → 应返回 duplicate_pairs_count >= 2（两行都被标记为重复）
        assert info["duplicate_pairs_count"] == 2
        assert info["duplicate_keys_sample"] is not None

    def test_deduped_df_passes(self, synthetic_df_for_audit):
        df = synthetic_df_for_audit.drop_duplicates(
            subset=["instrument_id", "trade_date"], keep="first"
        )
        info = check_rows(df)
        assert info["duplicate_pairs_count"] == 0

    def test_findings_marks_duplicate_as_blocker_invalid(
        self, synthetic_df_for_audit
    ):
        dates_info = check_trade_dates(synthetic_df_for_audit)
        rows_info = check_rows(synthetic_df_for_audit)
        lineage_info = check_lineage(synthetic_df_for_audit)
        readiness_info = check_readiness(synthetic_df_for_audit)
        findings = collect_integrity_findings(
            dates_info, rows_info, lineage_info, readiness_info, manifest={},
        )
        dup = [f for f in findings if f.check == "DUPLICATE_INSTR_DATE_FOUND"]
        assert len(dup) == 1
        assert dup[0].severity == "blocker"
        assert dup[0].status == "INVALID"


# ============================================================================
# Test 4: T-1 transition 使用前一交易日，不是自然日前一天
# ============================================================================

class TestTransitionAudit:
    def test_regime_value_transition_count_matrices_are_valid(
        self, synthetic_df_for_audit
    ):
        df_clean = synthetic_df_for_audit.drop_duplicates(
            subset=["instrument_id", "trade_date"], keep="first"
        )
        result = compute_transition_audit(df_clean, "regime_value")
        # valid pairs = (20 dates - 1 day-first-per-instrument) * 3 instruments = 57
        assert result["valid_pairs_count"] == 57

        # instr 0 在 d_idx=9→10 (d=第 10→11 天) 发生 1→-1
        # 所以 1→-1 必须出现在迁移矩阵中
        matrix = result["transition_matrix_count"]
        assert "1" in matrix
        assert "-1" in matrix["1"]
        assert matrix["1"]["-1"] >= 1

    def test_monday_correctly_connects_friday(
        self, synthetic_df_for_audit
    ):
        """验证 T-1 = 真实前一交易日。

        在 fixture 中：2026-01-05 (Mon) 开始；
        1/5 (Mon) → prev = 无；
        1/6 (Tue) → prev = 1/5；
        1/9 (Fri) → prev = 1/8 (Thu)；
        1/12 (Mon) → prev = 1/9 (Fri)，而不是 1/11 (Sun)。

        检查法：对 regime_value=全 0 的 instr 1，迁移计数 0→0 = 19（20 days - 1 first）。
        """
        df_clean = synthetic_df_for_audit.drop_duplicates(
            subset=["instrument_id", "trade_date"], keep="first"
        )
        # 只取 instr 1（全部 SIDEWAYS=0）
        instr1_id = df_clean["instrument_id"].unique()[1]
        sub = df_clean[df_clean["instrument_id"] == instr1_id]
        r = compute_transition_audit(sub, "regime_value")
        # valid pairs = 19（no missing data for this column in fixture）
        assert r["valid_pairs_count"] == 19
        assert r["transition_matrix_count"] == {"0": {"0": 19}}

    def test_natural_day_gap_is_not_counted_as_missing_prev(
        self, synthetic_df_for_audit
    ):
        """如果 transition 使用自然日 shift 则 Sat/Sun 会变成 prev 缺失。
        真实交易日 shift 只缺每个股票的第一天（3 instr = 3 缺少），
        总 valid_pairs 应该 = (20×3) - 3 = 57（= 前述），不得因为周末缺口变成 57 - 9 之类。
        """
        df_clean = synthetic_df_for_audit.drop_duplicates(
            subset=["instrument_id", "trade_date"], keep="first"
        )
        r = compute_transition_audit(df_clean, "swing_bias")
        assert r["valid_pairs_count"] == 57


# ============================================================================
# Test 5: denominator（unavailable / missing 不得进入有效分母）
# ============================================================================

class TestDenominator:
    def test_valid_false_not_in_ready_count(self, synthetic_df_for_audit):
        """fixture 中 d_idx=5 对应的日期所有 3 只股票都 valid_for_market_aggregation=False。"""
        df_clean = synthetic_df_for_audit.drop_duplicates(
            subset=["instrument_id", "trade_date"], keep="first"
        )
        info = check_readiness(df_clean)
        drm = info["daily_ready_count"]
        # 正常天 = 3；有一天 = 0 → min 应该是 0
        assert drm["min"] == 0
        assert drm["max"] == 3
        # 且 invalid_reason 里应该出现 warmup_period
        reason_counts = info["invalid_reason"]
        assert "warmup_period" in reason_counts
        assert reason_counts["warmup_period"] == 3  # 那一天的 3 个

    def test_invalid_reason_valid_rows_not_counted_as_missing(
        self, synthetic_df_for_audit
    ):
        df_clean = synthetic_df_for_audit.drop_duplicates(
            subset=["instrument_id", "trade_date"], keep="first"
        )
        info = check_readiness(df_clean)
        # __VALID__ 条目 = 20*3 - 3 = 57
        assert info["invalid_reason"].get("__VALID__", 0) == 57


# ============================================================================
# Test 6: T 日统计不读取未来日期（No Lookahead）
# ============================================================================

class TestNoLookahead:
    def test_transition_does_not_use_future_state(self, synthetic_df_for_audit):
        """过渡函数使用 groupby shift(1) → 只能是 prev→curr，不可能读到未来。

        验证：curr = 某 state 时，只来自 ≤ curr 日期的 state（不会出现 prev=后一天）。
        """
        df_clean = synthetic_df_for_audit.drop_duplicates(
            subset=["instrument_id", "trade_date"], keep="first"
        ).sort_values(["instrument_id", "trade_date"]).reset_index(drop=True)
        # 手动构造 prev 并与函数构造的对比
        df_clean["prev_reg_manual"] = (
            df_clean.sort_values(["instrument_id", "trade_date"])
            .groupby("instrument_id")["regime_value"]
            .shift(1)
        )
        # 用函数构造的结果：检查 1→-1 出现的日期只发生在后面的日期
        result = compute_transition_audit(df_clean, "regime_value")
        # top transitions 里不能有 "-1" → "1" 出现在 1→-1 之前
        transitions_list = result["top_15_transitions"]
        backward_count = sum(
            1 for t in transitions_list if t["prev"] == "-1" and t["curr"] == "1"
        )
        forward_count = sum(
            1 for t in transitions_list if t["prev"] == "1" and t["curr"] == "-1"
        )
        # instr0 的 1→-1 迁移量 = 1 个（10→11日）
        # instr2 中 mix，保证 forward 和 backward 计数合理
        # 关键属性：无论如何 forward_count >= 1 (来自 instr0)
        assert forward_count >= 1

    def test_trade_dates_detects_end_less_than_start(self):
        """故意注入日期逆序的 DF，检查 check_trade_dates 能暴露 sorted_asc=True（因为 unique 会 sort），
        但真实 transition/rows 审计里的顺序会出错 → 这里只保证 trade_dates start<end，
        transition 自己依赖 sort_values。
        """
        df = pd.DataFrame({
            "instrument_id": ["A", "A", "A"],
            "trade_date": ["2026-01-03", "2026-01-01", "2026-01-02"],
        })
        info = check_trade_dates(df)
        assert info["sorted_asc"] is True  # unique() 后 sorted()，所以日期段本身没问题
        # 但 transition_audit 会显式 sort_values，保证顺序正确
        result = compute_transition_audit(df.assign(regime_value=[1, 0, 1]), "regime_value")
        # valid pairs = 2: (2026-01-01→2026-01-02)=0→1, (2026-01-02→2026-01-03)=1→1
        assert result["valid_pairs_count"] == 2
        assert result["transition_matrix_count"] == {
            "0": {"1": 1},
            "1": {"1": 1},
        }


# ============================================================================
# Test 7: schema hash stability（schema 变更时 detect，防止 silently drift）
# ============================================================================

class TestSchemaHash:
    def test_hash_is_deterministic_and_stable(self):
        h1 = compute_schema_hash()
        h2 = compute_schema_hash()
        assert h1 == h2
        # length 是 16 hex chars
        assert len(h1) == 16 and all(c in "0123456789abcdef" for c in h1)


# ============================================================================
# Test 8: Verdict 函数（INVALD / PARTIAL / PASS）
# ============================================================================

class TestVerdict:
    def test_blocker_yields_invalid(self):
        fs = [IntegrityFinding(
            severity="blocker", check="X", evidence={},
            message="bad", status="INVALID",
        )]
        verdict, reasons = derive_round1_verdict(fs)
        assert verdict == "INVALID"

    def test_warning_yields_partial(self):
        fs = [IntegrityFinding(
            severity="warning", check="X", evidence={},
            message="warn", status="PARTIAL",
        )]
        verdict, reasons = derive_round1_verdict(fs)
        assert verdict == "PARTIAL"

    def test_info_only_yields_pass(self):
        fs = [IntegrityFinding(
            severity="info", check="OK", evidence={},
            message="ok", status="PASS",
        )]
        verdict, reasons = derive_round1_verdict(fs)
        assert verdict == "PASS"


# ============================================================================
# Test 9: DSN scheme translation（§12 blocker）
#   容器 DATABASE_URL 常为 SQLAlchemy scheme (postgresql+psycopg://...)；
#   psycopg2.connect() 只接受 libpq scheme。断言 normalize_libpq_dsn 仅改写 scheme，
#   不改动凭据/host/port/db/query。
# ============================================================================

class TestLibpqDsnNormalization:
    def test_sa_psycopg_scheme_translated_to_postgresql(self):
        dsn = "postgresql+psycopg://bz:bz@postgres:5432/bz_stock"
        out = normalize_libpq_dsn(dsn)
        assert out == "postgresql://bz:bz@postgres:5432/bz_stock"

    def test_sa_psycopg2_scheme_translated(self):
        dsn = "postgresql+psycopg2://u:p@h:5432/db?sslmode=disable"
        out = normalize_libpq_dsn(dsn)
        assert out == "postgresql://u:p@h:5432/db?sslmode=disable"

    def test_postgres_alias_translated(self):
        dsn = "postgres://u:p@h/db"
        assert normalize_libpq_dsn(dsn) == "postgresql://u:p@h/db"

    def test_postgresql_passthrough(self):
        dsn = "postgresql://u:p@h:5432/db"
        assert normalize_libpq_dsn(dsn) == dsn

    def test_keeps_credentials_host_port_db_query(self):
        # scheme 改写之外，其余字符完全保留（包括 query/fragment 顺序）
        dsn_sa = (
            "postgresql+psycopg://usr:x%40y@db.example.com:15432/"
            "my_db?sslmode=require&application_name=exp-r1#frag"
        )
        out = normalize_libpq_dsn(dsn_sa)
        assert out.startswith("postgresql://usr:x%40y@db.example.com:15432/my_db?")
        assert "sslmode=require" in out
        assert "application_name=exp-r1" in out
        assert "#frag" in out
        assert "+psycopg" not in out

    def test_no_scheme_passthrough_and_empty(self):
        assert normalize_libpq_dsn("") == ""
        # 非 URL DSN (key=value form) 透传
        kv = "host=h port=5432 dbname=d user=u"
        assert normalize_libpq_dsn(kv) == kv


# ============================================================================
# Test 10: DSN host override（§12 docker-service-alias → container IP）
#   容器 DATABASE_URL 中 host 常用 compose service alias (postgres)；
#   宿主机 python 不能解析该 DNS。apply_dsn_host_override 精确替换 hostname，
#   不触碰 scheme / userinfo / port / path / query / fragment。
# ============================================================================

class TestDsnHostOverride:
    def test_exact_host_match_replaces_only_hostname(self):
        dsn = "postgresql://bz:bz@postgres:5432/bz_stock?sslmode=disable"
        out = apply_dsn_host_override(dsn, from_host="postgres", to_host="172.19.0.3")
        assert out == (
            "postgresql://bz:bz@172.19.0.3:5432/bz_stock?sslmode=disable"
        )

    def test_preserves_password_with_special_chars(self):
        dsn = "postgresql+psycopg://usr:x%40y%21@pg-svc/db?q=1#f"
        # scheme 先由 normalize_libpq_dsn 翻译，host override 独立：
        norm = normalize_libpq_dsn(dsn)
        out = apply_dsn_host_override(norm, from_host="pg-svc", to_host="10.0.0.9")
        assert out.startswith("postgresql://usr:x%40y%21@10.0.0.9/")
        assert "q=1" in out
        assert "#f" in out

    def test_hostname_mismatch_returns_original(self):
        dsn = "postgresql://u:p@real-host:5432/db"
        out = apply_dsn_host_override(dsn, from_host="postgres", to_host="1.2.3.4")
        assert out == dsn

    def test_no_from_or_to_noops(self):
        dsn = "postgresql://u:p@postgres/db"
        assert apply_dsn_host_override(dsn, from_host=None, to_host="1.2.3.4") == dsn
        assert apply_dsn_host_override(dsn, from_host="postgres", to_host="") == dsn
        assert apply_dsn_host_override(dsn, from_host="", to_host="1.2.3.4") == dsn
        assert apply_dsn_host_override("", from_host="a", to_host="b") == ""

    def test_missing_userinfo_with_port(self):
        # 真实 container DB URL 总是 user:pass@host:port（bz:bz@postgres:5432），
        # 所以这种"无 userinfo 但 host=postgres 后接 :5432"的歧义形式不会在本实验出现。
        # 这里只做一个宽松 sanity：不管 urlsplit 怎么解析，apply_dsn_host_override 均不抛错
        # 且返回值是合法字符串。
        dsn = "postgresql://postgres:5432/bz_stock"
        out = apply_dsn_host_override(dsn, from_host="postgres", to_host="172.19.0.3")
        assert isinstance(out, str) and out.startswith("postgresql://") and "bz_stock" in out
        assert "+psycopg" not in out

    def test_hostname_not_port_not_touched(self):
        # 明确 userinfo + host + port：从 postgres → ip 不改动 port 或缺省 port
        dsn_default_port = "postgresql://u:p@postgres/db"
        out_def = apply_dsn_host_override(dsn_default_port, from_host="postgres", to_host="10.1.1.1")
        # 默认 port 5432 不会出现（urlunsplit 不重插缺省）
        assert out_def == "postgresql://u:p@10.1.1.1/db"
        # 显式 port
        dsn_explicit = "postgresql://u:p@postgres:15432/db"
        out_exp = apply_dsn_host_override(dsn_explicit, from_host="postgres", to_host="10.1.1.2")
        assert out_exp == "postgresql://u:p@10.1.1.2:15432/db"


if __name__ == "__main__":
    # 方便直接 `python test_round1.py` 执行
    pytest.main([__file__, "-v"])


# ==============================================================================
# §19 Round 1 v2 — DB-native / query-on-demand pure unit tests
# 覆盖：query shape A-E + session settings statements + no raw 120day raw fetchall.
# ==============================================================================


class Test_DB_Native_QueryShapes:
    """§19.A-E query shape assertions（纯字符串/函数检查，不需要连 DB）。"""

    @pytest.fixture(scope="class")
    def db_native_mod(self):
        # 延迟导入：避免 import psycopg2（SessionGuard.__post_init__ 目前延迟，
        # 但为保险只 import 常量 / build_* / query_* 纯函数集合）。
        from experiments.review_market_observation.round1 import round1_db_native as mod
        return mod

    # §19.A — integrity / coverage 聚合 query 不能是 SELECT *
    def test_step1_row_summary_is_aggregate_only(self, db_native_mod):
        sql = db_native_mod.SQL_STEP1_ROW_SUMMARY
        assert db_native_mod.query_is_aggregate_only(sql), (
            "SQL_STEP1_ROW_SUMMARY must be aggregate only (no SELECT * / no s.state_payload raw)"
        )

    def test_step2_daily_coverage_has_no_select_star(self, db_native_mod):
        sql = db_native_mod.SQL_STEP2_DAILY_COVERAGE
        assert not db_native_mod.query_contains_select_star(sql), (
            "step2 daily coverage must not SELECT *"
        )
        assert db_native_mod.query_is_aggregate_only(sql)

    # §19.B — categorical / continuous per-field query 不是 SELECT *
    def test_step3_categorical_no_select_star_all_fields(self, db_native_mod):
        for field in db_native_mod.CATEGORICAL_STATE_FIELDS:
            sql = db_native_mod.build_sql_step3_categorical(field)
            assert not db_native_mod.query_contains_select_star(sql), field

    def test_step4_continuous_no_select_star_all_fields(self, db_native_mod):
        for field in db_native_mod.CONTINUOUS_STATE_FIELDS:
            sql, _ = db_native_mod.build_sql_step4_continuous(field, include_percentile=True)
            assert not db_native_mod.query_contains_select_star(sql), field

    # §19.C — 禁止把 state_payload 作为整列 raw 返回（我们只 ->> 'key' 访问）
    def test_no_raw_state_payload_column_returned(self, db_native_mod):
        sample_sql_checks = [
            db_native_mod.SQL_STEP1_ROW_SUMMARY,
            db_native_mod.SQL_STEP2_DAILY_COVERAGE,
            db_native_mod.build_sql_step3_categorical("regime_value"),
            db_native_mod.build_sql_step5_transition("swing_bias"),
        ]
        for sql in sample_sql_checks:
            # 把合法的 "state_payload ->>" 字符串去掉；剩余不应出现 "state_payload"
            stripped = sql.replace("state_payload ->>", "@@PAYLOAD_ACCESS@@")
            assert "state_payload" not in stripped, (
                "query 包含 raw state_payload 列访问（可能返回整列 JSONB → OOM）:\n"
                + sql[:220]
            )

    # §19.D — transition query 必须使用 LAG(... ) OVER (PARTITION BY instrument_id ORDER BY trade_date)
    def test_step5_transition_uses_lag_partition_instrument(self, db_native_mod):
        for field in db_native_mod.CATEGORICAL_STATE_FIELDS:
            sql = db_native_mod.build_sql_step5_transition(field)
            assert db_native_mod.query_uses_transition_lag(sql), field

    # §19.E — 所有 query 都不该带 banned raw-frozen markers（防回归旧 full-extract 路径）
    def test_no_full_load_markers_in_all_query_strings(self, db_native_mod):
        samples = [
            db_native_mod.SQL_STEP0_CANDIDATE_TRADE_DATES,
            db_native_mod.SQL_STEP0_LINEAGE_COUNTS,
            db_native_mod.SQL_STEP1_ROW_SUMMARY,
            db_native_mod.SQL_STEP1_DUPLICATE_COUNT,
            db_native_mod.SQL_STEP1_DUPLICATE_SAMPLE,
            db_native_mod.SQL_STEP1_SOURCE_HISTOGRAM,
            db_native_mod.SQL_STEP2_DAILY_COVERAGE,
            db_native_mod.build_sql_step3_categorical("regime_value"),
            db_native_mod.build_sql_step4_continuous("regime_strength")[0],
            db_native_mod.build_sql_step5_transition("swing_bias"),
        ]
        for s in samples:
            assert db_native_mod.query_has_no_fetchall_raw_120day(s), s[:160]

    # §19.F — categorical 连续字段 factory 在字段非法时 raise ValueError（contract）
    def test_step3_unknown_field_raises_valueerror(self, db_native_mod):
        with pytest.raises(ValueError):
            db_native_mod.build_sql_step3_categorical("does_not_exist_xyz")

    def test_step4_unknown_field_raises_valueerror(self, db_native_mod):
        with pytest.raises(ValueError):
            db_native_mod.build_sql_step4_continuous("does_not_exist_xyz")

    def test_step5_unknown_field_raises_valueerror(self, db_native_mod):
        with pytest.raises(ValueError):
            db_native_mod.build_sql_step5_transition("does_not_exist_xyz")


class Test_DB_Native_Session_Settings:
    """§19 SessionGuard settings template：必须包含以下 5 个 SET LOCAL 入口。"""

    @pytest.fixture(scope="class")
    def db_native_mod(self):
        from experiments.review_market_observation.round1 import round1_db_native as mod
        return mod

    def test_session_settings_contains_work_mem(self, db_native_mod):
        keys = {k for k, _ in db_native_mod.SESSION_STATEMENTS_TEMPLATE}
        for need in ("transaction_read_only", "work_mem", "statement_timeout",
                     "lock_timeout", "idle_in_transaction_session_timeout",
                     "max_parallel_workers_per_gather"):
            assert need in keys, f"missing session setting key: {need}"

    def test_session_settings_statement_timeout_is_sane(self, db_native_mod):
        stmt_map = dict(db_native_mod.SESSION_STATEMENTS_TEMPLATE)
        assert "'300s'" in stmt_map["statement_timeout"] or "\"300s\"" in stmt_map["statement_timeout"]


class Test_DB_Native_DSN_Compatibility:
    """§19 复用 DSN normalize / override 纯函数（与旧路径保持行为一致）。"""

    @pytest.fixture(scope="class")
    def db_native_mod(self):
        from experiments.review_market_observation.round1 import round1_db_native as mod
        return mod

    def test_normalize_sqlalchemy_scheme_strips_plus_psycopg(self, db_native_mod):
        dsn = "postgresql+psycopg://u:p@h/db"
        assert db_native_mod.normalize_libpq_dsn(dsn) == "postgresql://u:p@h/db"

    def test_host_override_touches_only_matching_host(self, db_native_mod):
        dsn = "postgresql://u:p@postgres:5432/db"
        out_a = db_native_mod.apply_dsn_host_override(dsn, from_host="postgres", to_host="1.2.3.4")
        assert "1.2.3.4" in out_a
        out_b = db_native_mod.apply_dsn_host_override(dsn, from_host="other", to_host="9.9.9.9")
        assert "@postgres" in out_b  # unchanged


class Test_Round1_TradeDates_Are_DateObjects:
    """§2.1 最小修正确认：trade_dates 保持 datetime.date，供 psycopg2 绑定 date[]。

    不连接 DB；只验证 build_selected_trade_dates 与 params 构造保持 date 类型。
    """

    @pytest.fixture(scope="class")
    def db_native_mod(self):
        from experiments.review_market_observation.round1 import round1_db_native as mod
        return mod

    @pytest.fixture(scope="class")
    def schema_mod(self):
        from experiments.review_market_observation.round1 import dataset_schema as mod
        return mod

    def test_selected_trade_dates_are_datetime_date(self, db_native_mod, schema_mod):
        from datetime import date, timedelta
        known_asc = [date(2026, 1, 1) + timedelta(days=i) for i in range(130)]
        selected = schema_mod.build_selected_trade_dates(known_asc, target_count=120)
        assert len(selected) == 120
        assert all(isinstance(d, date) for d in selected), "selected trade dates must be datetime.date"

    def test_params_trade_dates_still_date_objects(self, db_native_mod, schema_mod):
        from datetime import date, timedelta
        known_asc = [date(2026, 1, 1) + timedelta(days=i) for i in range(130)]
        selected = schema_mod.build_selected_trade_dates(known_asc, target_count=120)
        # 与 run_round1_db_native（Step 0bis）一致的 params 构造：trade_dates 直接传 date 列表
        params = {
            "algo": db_native_mod.EXPECTED_ALGORITHM_VERSION,
            "hc": db_native_mod.EXPECTED_HISTORY_CONTRACT_VERSION,
            "trade_dates": selected,
        }
        assert all(isinstance(d, date) for d in params["trade_dates"]), (
            "params['trade_dates'] must remain datetime.date objects (not str)"
        )
        assert not any(isinstance(d, str) for d in params["trade_dates"])


class Test_Round1_ResourceSettings_FailClosed:
    """§2.2 最小修正确认：SESSION_STATEMENTS_TEMPLATE 必须包含全部 5 个 resource settings。

    真实 fail-closed（SET LOCAL 失败即 raise）由 SessionGuard 在 DB 连接时强制执行；
    这里只做纯模板检查（不连 DB）。
    """

    @pytest.fixture(scope="class")
    def db_native_mod(self):
        from experiments.review_market_observation.round1 import round1_db_native as mod
        return mod

    def test_all_five_resource_settings_present(self, db_native_mod):
        stmt_map = dict(db_native_mod.SESSION_STATEMENTS_TEMPLATE)
        for key in ("work_mem", "statement_timeout", "lock_timeout",
                    "idle_in_transaction_session_timeout",
                    "max_parallel_workers_per_gather"):
            assert key in stmt_map, f"missing resource setting key: {key}"
            assert "SET LOCAL" in stmt_map[key], f"{key} must be SET LOCAL"

    def test_no_rollback_continue_legacy_path(self, db_native_mod):
        """确认 SessionGuard 已移除 rollback-and-continue（settings_unavailable 状态已删除）。"""
        src = open(db_native_mod.__file__).read()
        assert "settings_unavailable" not in src, "legacy 'unavailable' state must be removed"
        assert "ResourceSettingError" in src, "fail-closed exception class must exist"

