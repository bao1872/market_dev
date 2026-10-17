"""Read-only production DB -> canonical QFQ Parquet input for the strategy worker.

Only invoked by an explicitly authorized separate worker, not FastAPI imports.
No database writes, migrations or external market provider fallback.

Prices come from the single market-data owner
(`MarketDataAggregationService.get_bars_batch`, adj="qfq"). This module never
implements or re-applies an adjustment factor, so the export cannot silently be
raw bars with a "qfq" label.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.models.instrument import Instrument
from app.services.instrument_maintenance_service import stock_symbol_sql_filter

from .canonical_strategy_bars import load_official_qfq_stock_batch
from .producer import ProducerError

EXPORT_BARS = 'bars_daily_qfq.parquet'
EXPORT_INSTRUMENTS = 'instruments.parquet'
EXPORT_MANIFEST = 'input_manifest.json'


async def read_official_qfq_stock_batch(
    as_of: date, *, batch_size: int = 80, min_bars: int = 150, with_factor_series: bool = True,
    max_symbols: int = 0,
) -> tuple[dict, dict]:
    """Read canonical QFQ bars + authoritative factor series directly from the
    single market-data owner (MDAS). No Parquet round-trip, no legacy
    OFFLINE_FACTOR_DIAGNOSTIC fallback. Returns (accepted_frames, report).

    This is the production-reading contract reused by both the offline Parquet
    export and the in-place canonical source mode; keep all MDAS access here so
    the canonical mode never re-implements or re-applies an adjustment factor.
    """
    from .readonly_runtime import assert_readonly_session, configure_readonly_connections
    configure_readonly_connections()
    async with AsyncSessionLocal() as db:
        readonly_verified = await assert_readonly_session(db)
        if not readonly_verified:
            raise ProducerError('STRATEGY_DATABASE_READ_ONLY_PROOF_REQUIRED')
        if max_symbols < 0:
            raise ProducerError('NEGATIVE_SYMBOL_LIMIT')
        statement = select(Instrument.id, Instrument.symbol, Instrument.market).where(
            stock_symbol_sql_filter(Instrument)).order_by(Instrument.market, Instrument.symbol)
        if max_symbols:
            statement = statement.limit(max_symbols)
        objects = (await db.execute(statement)).all()
        if not objects:
            raise ProducerError('NO_INSTRUMENTS')
        instrument_by_id = {
            x.id: f"{str(x.market).upper()}:{str(x.symbol).zfill(6)}" for x in objects
        }
        accepted, report = await load_official_qfq_stock_batch(
            db, instrument_by_id, as_of, batch_size=batch_size,
            min_bars=min_bars, with_factor_series=with_factor_series,
        )
        report['connection_read_only_verified'] = readonly_verified
        report['universe_owner'] = 'instrument_maintenance_service.stock_symbol_sql_filter'
        import os
        report_path = os.getenv('PANJI_STRATEGY_SOURCE_REPORT_FILE')
        if report_path:
            from .release_store import atomic_json
            atomic_json(Path(report_path), report)
        if not accepted:
            raise ProducerError('NO_ELIGIBLE_STOCKS')
        return accepted, report


async def export_dataset(output: Path, as_of: date, *, batch_size: int = 80) -> dict:
    accepted, report = await read_official_qfq_stock_batch(as_of, batch_size=batch_size)
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    file = output / EXPORT_BARS
    inst_path = output / EXPORT_INSTRUMENTS
    manifest_path = output / EXPORT_MANIFEST
    if file.exists() or inst_path.exists() or manifest_path.exists():
        raise ProducerError('EXPORT_OUTPUT_MUST_BE_EMPTY')
    frames = []
    for symbol, frame in accepted.items():
        one = frame.copy()
        one.insert(1, 'symbol', symbol)
        frames.append(one)
    all_bars = pd.concat(frames, ignore_index=True)
    if 'adj_factor' not in all_bars.columns:
        # The authoritative factor series is REQUIRED for point-in-time feature
        # rebuilding; an export without it cannot claim PIT support.
        raise ProducerError('CANONICAL_EXPORT_MISSING_FACTOR_SERIES')
    all_bars.to_parquet(file, compression='zstd', index=False)
    pd.DataFrame({'symbol': sorted(accepted)}).to_parquet(
        inst_path, compression='zstd', index=False,
    )
    manifest = {
        'schema': 'panji-strategy-input-qfq-v1',
        'price_adjustment': 'qfq',
        'adjustment_as_of': as_of.isoformat(),
        'source_owner': report['source_owner'],
        'source_policy': report['source_policy'],
        'completed_only': report['completed_only'],
        'external_fetch_allowed': report['external_fetch_allowed'],
        'db_write_allowed': report['db_write_allowed'],
        'total_instruments': report['total_instruments'],
        'eligible_stock_count': report['eligible_stock_count'],
        'covered_asof_count': report['covered_asof_count'],
        'coverage_ratio_of_eligible': report['coverage_ratio_of_eligible'],
        'coverage_ratio_of_total': report['coverage_ratio_of_total'],
        'excluded': report['excluded'],
        'excluded_symbols': report.get('excluded_symbols', {}),
        'stale_symbols': report.get('stale_symbols', []),
        'expected_symbols': report.get('expected_symbols', []),
        'provenance': report['provenance'],
        'rows': int(len(all_bars)),
        'factor_provenance_authoritative': True,
        'pit_factor_series_available': report.get('pit_factor_series_available', False),
        'pit_factor_series_symbols': report.get('pit_factor_series_symbols', 0),
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, allow_nan=False),
        encoding='utf-8',
    )
    return {
        'stocks': len(accepted), 'rows': int(len(all_bars)), 'as_of': str(as_of),
        'bars': str(file), 'instruments': str(inst_path), 'manifest': str(manifest_path),
        'price_adjustment': 'qfq', 'db_writes': 0, 'source_report': report,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--as-of', type=date.fromisoformat, required=True)
    p.add_argument('--output-dir', type=Path, required=True)
    x = p.parse_args()
    print(asyncio.run(export_dataset(x.output_dir, x.as_of)))


if __name__ == '__main__':
    main()
