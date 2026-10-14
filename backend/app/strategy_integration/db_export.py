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
from datetime import date
import json
from pathlib import Path

import pandas as pd
from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.models.instrument import Instrument
from .producer import ProducerError

EXPORT_BARS = 'bars_daily_qfq.parquet'
EXPORT_INSTRUMENTS = 'instruments.parquet'
EXPORT_MANIFEST = 'input_manifest.json'


async def export_dataset(output: Path, as_of: date, *, batch_size: int = 80) -> dict:
    from .canonical_strategy_bars import load_official_qfq_stock_batch
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    file = output / EXPORT_BARS
    inst_path = output / EXPORT_INSTRUMENTS
    manifest_path = output / EXPORT_MANIFEST
    if file.exists() or inst_path.exists() or manifest_path.exists():
        raise ProducerError('EXPORT_OUTPUT_MUST_BE_EMPTY')
    async with AsyncSessionLocal() as db:
        objects = (await db.execute(
            select(Instrument.id, Instrument.symbol, Instrument.market)
        )).all()
    if not objects:
        raise ProducerError('NO_INSTRUMENTS')
    instrument_by_id = {
        x.id: f"{str(x.market).upper()}:{str(x.symbol).zfill(6)}" for x in objects
    }
    async with AsyncSessionLocal() as db:
        accepted, report = await load_official_qfq_stock_batch(
            db, instrument_by_id, as_of, batch_size=batch_size,
        )
    if not accepted:
        raise ProducerError('NO_ELIGIBLE_STOCKS')
    frames = []
    for symbol, frame in accepted.items():
        one = frame.copy()
        one.insert(1, 'symbol', symbol)
        frames.append(one)
    all_bars = pd.concat(frames, ignore_index=True)
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
        'provenance': report['provenance'],
        'rows': int(len(all_bars)),
        'factor_provenance_authoritative': True,
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
