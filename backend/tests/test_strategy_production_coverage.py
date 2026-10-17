"""Full-market gates use the independent source universe, never the surviving pool."""
import pytest
from app.strategy_integration.cli import validate_source_coverage
from app.strategy_integration.producer import ProducerError


def report(total=5920, eligible=5265, covered=5259):
    return dict(total_instruments=total, eligible_stock_count=eligible,
                covered_asof_count=covered, coverage_ratio=covered/max(1,eligible))


def test_current_baseline_passes_both_denominators():
    validate_source_coverage(report(), 0)


def test_many_missing_stocks_cannot_disappear_from_denominator():
    with pytest.raises(ProducerError, match='FULL_UNIVERSE_TRADE_DAY_COVERAGE'):
        validate_source_coverage(report(eligible=1500, covered=1500), 0)


@pytest.mark.parametrize('field,value', [('total_instruments', None), ('total_instruments', 0),
                                        ('covered_asof_count', 6000), ('eligible_stock_count', True)])
def test_inconsistent_source_counts_fail_closed(field, value):
    data = report()
    data[field] = value
    with pytest.raises(ProducerError, match='DENOMINATOR_INVALID'):
        validate_source_coverage(data, 0)


def test_explicit_diagnostic_subset_is_not_full_market_evidence():
    validate_source_coverage(report(eligible=1, covered=1), 1)
