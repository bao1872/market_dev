"""测试 CLI — 参数解析、dry-run、只读 SQL 设置。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.research.regime_discovery.data_access import STATEMENT_TIMEOUT_SECONDS


class TestParseArgs:
    def test_defaults(self):
        import sys

        from scripts.research_regime_discovery import parse_args
        original = sys.argv
        sys.argv = ["research_regime_discovery"]
        try:
            args = parse_args()
            assert args.dry_run is False
            assert args.start is None
            assert args.end is None
            assert args.sample_rows == 150000
            assert args.seed == 42
            assert args.k_min == 3
            assert args.k_max == 8
            assert args.chunk_size == 25000
            assert args.max_rss_mb == 1500
            assert args.representation == "both"
            assert args.output_dir == "/home/ubuntu/panji_research_outputs/regime_discovery"
        finally:
            sys.argv = original

    def test_representation_choices(self):
        import sys

        from scripts.research_regime_discovery import parse_args
        original = sys.argv
        for rep in ["absolute", "cross_sectional", "both"]:
            sys.argv = ["research_regime_discovery", "--representation", rep]
            args = parse_args()
            assert args.representation == rep
        sys.argv = original

    def test_invalid_representation_rejected(self):
        import sys

        from scripts.research_regime_discovery import parse_args
        original = sys.argv
        sys.argv = ["research_regime_discovery", "--representation", "invalid"]
        try:
            with pytest.raises(SystemExit):
                parse_args()
        finally:
            sys.argv = original

    def test_k_min_greater_than_k_max_rejected(self):
        import sys

        from scripts.research_regime_discovery import parse_args
        original = sys.argv
        sys.argv = ["research_regime_discovery", "--k-min", "5", "--k-max", "3"]
        try:
            with pytest.raises(SystemExit):
                parse_args()
        finally:
            sys.argv = original

    def test_k_min_below_2_rejected(self):
        import sys

        from scripts.research_regime_discovery import parse_args
        original = sys.argv
        sys.argv = ["research_regime_discovery", "--k-min", "1"]
        try:
            with pytest.raises(SystemExit):
                parse_args()
        finally:
            sys.argv = original


class TestDryRun:
    def test_exits_without_db(self, caplog):
        """dry-run 应不查 DB 不写文件，返回 0。"""
        import sys

        from scripts.research_regime_discovery import main
        original = sys.argv
        sys.argv = ["research_regime_discovery", "--dry-run"]
        try:
            with caplog.at_level("INFO"):
                rc = main()
            assert rc == 0
            # 日志中应有 [dry-run]
            log_text = " ".join(r.message for r in caplog.records)
            assert "dry-run" in log_text or "dry_run" in log_text
        finally:
            sys.argv = original


class TestSampleVsFullAssignmentMode:
    """验证 CLI 区分 sample（Phase A-D）和 full-assignment（Phase E）模式。"""

    def test_dry_run_mentions_both_phases(self, caplog):
        """dry-run 日志应同时提及 sample 阶段和全量 assignment 阶段。"""
        import sys

        from scripts.research_regime_discovery import main
        original = sys.argv
        sys.argv = ["research_regime_discovery", "--dry-run"]
        try:
            with caplog.at_level("INFO"):
                rc = main()
            assert rc == 0
            log_text = " ".join(r.message for r in caplog.records)
            # sample 阶段
            assert "分层抽样" in log_text or "sample" in log_text.lower()
            # 全量 assignment 阶段
            assert "全量 assignment" in log_text or "get_all_matrix_rows" in log_text
        finally:
            sys.argv = original

    def test_sample_rows_controls_sample_size(self):
        """--sample-rows 参数控制抽样行数。"""
        import sys

        from scripts.research_regime_discovery import parse_args
        original = sys.argv
        sys.argv = ["research_regime_discovery", "--sample-rows", "50000"]
        try:
            args = parse_args()
            assert args.sample_rows == 50000
        finally:
            sys.argv = original

    def test_full_assignment_uses_get_all_matrix_rows(self, caplog):
        """dry-run 应说明全量 assignment 使用 get_all_matrix_rows（完整横截面 rank）。"""
        import sys

        from scripts.research_regime_discovery import main
        original = sys.argv
        sys.argv = ["research_regime_discovery", "--dry-run"]
        try:
            with caplog.at_level("INFO"):
                main()
            log_text = " ".join(r.message for r in caplog.records)
            assert "get_all_matrix_rows" in log_text or "完整横截面" in log_text
        finally:
            sys.argv = original


class TestReadonlySQLSession:
    """测试 get_session 设置了 statement_timeout 和 read_only。"""

    def test_sets_statement_timeout(self):
        """验证 STATEMENT_TIMEOUT_SECONDS 常量。"""
        assert STATEMENT_TIMEOUT_SECONDS == 120

    def test_get_session_sets_read_only(self):
        """测试 get_session 调用 SET default_transaction_read_only。"""
        from sqlalchemy.engine import Engine

        from app.research.regime_discovery.data_access import get_session

        # Mock engine 和 session
        mock_engine = MagicMock(spec=Engine)
        mock_session = MagicMock()
        executed_sqls: list[str] = []

        def fake_execute(stmt, *args, **kwargs):
            executed_sqls.append(str(stmt))
            return MagicMock()

        mock_session.execute = fake_execute
        # sessionmaker 返回 mock_session
        with patch("app.research.regime_discovery.data_access.sessionmaker") as mock_sm:
            mock_sm.return_value.return_value = mock_session
            _session = get_session(mock_engine)
            # 验证执行了 SET statement_timeout 和 SET default_transaction_read_only
            assert any("statement_timeout" in s for s in executed_sqls), \
                f"未执行 statement_timeout，实际: {executed_sqls}"
            assert any("default_transaction_read_only" in s for s in executed_sqls), \
                f"未执行 read_only，实际: {executed_sqls}"
