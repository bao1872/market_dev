"""Actual native-weight integration regression; unittest permits isolated ML runtime.

This is synthetic local evidence, not remote runtime/real-price parity evidence.
"""
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import numpy as np
import pandas as pd

from app.strategy_integration import producer
from app.strategy_integration.fourteen_adapter import PACKAGE, load_package
from app.services.strategy_candidate_snapshot import validate, get_stock
from app.services.strategy_candidate_shards import write_candidate_shards, load_candidate_shards
from app.services.strategy_research_read_service import _check_snapshot
from app.services.strategy_task_performance import evaluate_tasks


class FourteenIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle, cls.facts = load_package()
        rng = np.random.default_rng(26)
        n = 160
        close = 25 + np.sin(np.arange(n)/7)*3 + np.cumsum(rng.normal(0,.2,n))
        cls.raw = pd.DataFrame({'datetime': pd.bdate_range('2025-01-02', periods=n),
            'open': close*.999, 'high': close*1.02, 'low': close*.98,
            'close': close, 'volume': np.full(n, 2e6)})

    def snapshots(self):
        return producer.make_snapshots({'SH:600000': self.raw}, self.bundle['fourteen_runtime'].engine,
            self.bundle, as_of=str(self.raw.datetime.iloc[-1].date()), source_sha='f'*64,
            chart_bars=45, release_id='a'*64)

    def test_package_sha_and_input_gate(self):
        manifest = json.loads((PACKAGE/'manifest.json').read_text())
        for path, sha in manifest['files'].items():
            self.assertEqual(hashlib.sha256((PACKAGE/path).read_bytes()).hexdigest(), sha)
        self.assertEqual(len(self.bundle['fourteen_runtime'].models), 14)
        with self.assertRaises(producer.ProducerError):
            producer.require_price_contract(self.bundle)

    def test_production_readers_and_shards(self):
        candidate, evaluation = self.snapshots()
        validate(candidate)
        _check_snapshot(evaluation)
        self.assertEqual(candidate['schema'], 'panji-strategy-candidates-v3')
        self.assertEqual(candidate['metadata']['scoring_architecture'], 'GLOBAL_FOURTEEN')
        stock = get_stock(candidate, 'SH:600000')
        self.assertNotIn('task_samples', stock)
        self.assertTrue(any(o['stage'] == 'D0' for c in stock['candidates'] for o in c['observations']))
        with tempfile.TemporaryDirectory() as folder:
            write_candidate_shards(candidate, Path(folder), validate=validate, target_bytes=1_000_000)
            self.assertEqual(load_candidate_shards(Path(folder)/'candidate_snapshot.json', validate=validate), candidate)
        self.assertEqual(evaluate_tasks(evaluation)['status'], 'NO_SAMPLES')
        self.assertEqual(evaluate_tasks(evaluation, period='UNASSIGNED')['status'], 'AVAILABLE')

    def test_fourteen_training_uses_valid_matured_split_samples(self):
        from app.strategy_integration.fourteen_training import build_dataset
        from app.strategy_integration.rolling_windows import rolling_windows
        from datetime import date
        windows = rolling_windows(date(2026, 10, 16))
        rows = build_dataset({'SH:600000': self.raw}, self.bundle['fourteen_runtime'], windows)
        self.assertTrue(rows)
        self.assertTrue(any(key[1] == 'D0' for key in rows))
        for (split, layer, kind, stage), samples in rows.items():
            for row in samples:
                self.assertLess(row['decision_date'], row['label_available_at'])
                self.assertLessEqual(row['label_available_at'], windows['dates'][split][1])
                self.assertIn(row['label'], (0, 1))
        from app.strategy_integration.training_spool import DatasetSpool
        with tempfile.TemporaryDirectory() as folder:
            spool = DatasetSpool(Path(folder), len(self.bundle['fourteen_runtime'].engine.ALL_COLS))
            for key, samples in rows.items():
                for row in samples:
                    spool.append(key, row)
            for key, samples in rows.items():
                x, y = spool.partitions[key].as_arrays()
                np.testing.assert_array_equal(x, np.stack([r['features'] for r in samples]))
                np.testing.assert_array_equal(y, [r['label'] for r in samples])

    def test_prefix_replay_is_stable(self):
        from app.strategy_integration.fourteen_adapter import build_stock
        first, _ = build_stock(self.raw.iloc[:150], 'SH:600000', self.bundle,
            release_id='a'*64, chart_bars=160)
        full, _ = build_stock(self.raw, 'SH:600000', self.bundle,
            release_id='a'*64, chart_bars=160)
        upto = str(self.raw.datetime.iloc[149].date())
        expected = {c['candidate_id']: c for c in first['candidates']}
        actual = {c['candidate_id']: {**c, 'observations': [o for o in c['observations'] if o['decision_date'] <= upto]}
            for c in full['candidates'] if c['visible_at'] <= upto}
        self.assertEqual(actual, expected)

    def test_prefix_cache_reuses_only_identical_inputs(self):
        from app.strategy_integration.inference_cache import decision_inputs
        runtime = self.bundle['fourteen_runtime']
        raw = self.raw.iloc[:150].copy()
        day = str(raw.datetime.iloc[-1].date())
        with tempfile.TemporaryDirectory() as folder, patch.dict('os.environ', {'PANJI_STRATEGY_CACHE_ROOT': folder}):
            expected = decision_inputs(runtime, raw, day, 'SH:600000')
            from app.services.strategy_tv_zigzag import current_segment
            self.assertEqual(expected[3], current_segment(raw))
            _, original, _ = runtime.engine.replay(raw)
            for i in range(146,150):
                for kind in ('H','L'):
                    self.assertEqual(producer._reference(raw, expected[1], i, kind), producer._reference(raw, original, i, kind))
            with patch.object(runtime, 'score', side_effect=AssertionError('identical prefix must use cache')):
                self.assertEqual(decision_inputs(runtime, raw, day, 'SH:600000'), expected)
                changed = raw.copy()
                changed.loc[149, 'volume'] *= 1.1
                with self.assertRaisesRegex(AssertionError, 'identical prefix'):
                    decision_inputs(runtime, changed, day, 'SH:600000')

    def test_native_breakout_survives_replacement_without_recounting(self):
        raw = self.raw.copy()
        close = raw.close.to_numpy().copy()
        close[130:140] = np.linspace(25, 35, 10)
        close[140:151] = np.linspace(34, 20, 11)
        close[151:154] = [40, 18, 30]
        for key, factor in [('open', .999), ('high', 1.02), ('low', .98), ('close', 1)]:
            raw[key] = close*factor
        c, e = producer.make_snapshots({'SH:600000': raw}, self.bundle['fourteen_runtime'].engine,
            self.bundle, as_of=str(raw.datetime.iloc[-1].date()), source_sha='f'*64,
            chart_bars=120, release_id='a'*64)
        producer.verify_against_readers(c, e)
        anchor = str(raw.datetime.iloc[150].date())
        candidate = next(x for x in c['stocks']['SH:600000']['candidates']
                         if x['anchor_date'] == anchor and x['kind'] == 'L' and x['observations'][0]['stage'] == 'D1')
        rows = candidate['observations']
        self.assertEqual([o['first_breakout'] for o in rows], [True, False, False])
        self.assertEqual(rows[1]['scoring_status'], 'CANDIDATE_INACTIVE')
        self.assertIsNone(rows[1]['l2_score'])
        self.assertEqual(rows[0]['breakout']['event_id'], rows[2]['breakout']['event_id'])
        from app.services.strategy_tv_zigzag import current_segment
        self.assertEqual(rows[0]['breakout']['trend_at_breakout'], current_segment(raw.iloc[:152]))
        self.assertEqual(rows[1]['segment']['asOf'], rows[1]['decision_date'])
        self.assertEqual(rows[0]['breakout']['trend_at_breakout'], rows[1]['breakout']['trend_at_breakout'])

    def test_native_qfq_model_and_snapshot_publish_through_same_pointer_owner(self):
        import contextlib
        import io
        import shutil
        from types import SimpleNamespace
        from app.strategy_integration.fourteen_adapter import verify_production_package
        from app.strategy_integration.partitioned_build import build
        from app.strategy_integration.release_store import atomic_json, activate_batch, digest_path, resolve_active_release
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            root = Path(folder)
            package = root/'model'
            shutil.copytree(PACKAGE,package,ignore=shutil.ignore_patterns('__pycache__'))
            manifest = json.loads((package/'manifest.json').read_text())
            manifest['price_input_contract'] = 'QFQ_ASOF_V1'
            atomic_json(package/'manifest.json',manifest)
            sha = digest_path(package/'manifest.json')
            verify_production_package(package,sha)
            bundle, facts = load_package(package,sha)
            release_id = producer.release_id_for(sha)
            snapshots = root/'snapshots'
            from app.strategy_integration import cli
            args = SimpleNamespace(as_of=str(self.raw.datetime.iloc[-1].date()),chart_bars=90,
                output_dir=snapshots,assets=PACKAGE,candidate_bundle=package,candidate_sha=sha,
                personal_registry=None,max_symbols=1,expected_active_release_id=release_id,
                expected_active_bundle_sha='0'*64)
            with patch.object(cli,'_load_market',side_effect=AssertionError('must reject changed model before market reads')):
                with self.assertRaisesRegex(producer.ProducerError,'PINNED_MODEL_CHANGED'):
                    cli.build(args)
            args.expected_active_bundle_sha = sha
            with patch.object(cli,'_load_market',return_value=({'SH:600000':self.raw},None,{'source_sha256':'f'*64})):
                result = cli.build(args)
            # Pure temporary-filesystem publication contract, not a production approval.
            activate_batch(snapshots,Path(result['output']))
            active = resolve_active_release(snapshots)
            self.assertEqual(active['bundle_sha256'],sha)
            self.assertEqual(active['release_id'],release_id)
            self.assertEqual(Path(active['batch']), (snapshots/'current').resolve())

    def test_lab_golden_verifier_accepts_parity_and_rejects_changed_score(self):
        import shutil
        from app.strategy_integration.lab_delivery import verify_golden
        from app.strategy_integration.release_store import atomic_json, digest_path
        runtime = self.bundle['fourteen_runtime']
        cases, seen = [], set()
        for end in range(21, len(self.raw)+1):
            bars = json.loads(self.raw.iloc[:end].to_json(orient='records', date_format='iso'))
            raw = runtime.prepare(pd.DataFrame(bars))
            day = str(raw.datetime.iloc[-1].date())
            expected = runtime.score(raw, day)
            tasks = {v['task'] for v in expected['signals']}
            if tasks - seen:
                _, snap, _ = runtime.engine.replay(raw)
                _, features = runtime.engine.features(raw, snap, [len(raw)-1])
                values = [[float(x) if np.isfinite(x) else None for x in row] for row in features]
                cases.append(dict(bars=bars, as_of=day, features=values, expected=expected))
                seen.update(tasks)
        self.assertEqual(seen, set(runtime.manifest['models']))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)/'package'
            shutil.copytree(PACKAGE, root, ignore=shutil.ignore_patterns('__pycache__'))
            windows = {'train':['2024-01-01','2025-12-31'], 'val':['2026-01-01','2026-06-30'], 'test':['2026-07-01','2026-09-30']}
            manifest = {**runtime.manifest, 'price_input_contract':'QFQ_ASOF_V1', 'split_windows':windows}
            atomic_json(root/'manifest.json',manifest)
            sha = digest_path(root/'manifest.json')
            atomic_json(root/'golden_cases.json',cases)
            atomic_json(root/'validation_report.json', {'windows':{k:dict(start=v[0],end=v[1]) for k,v in windows.items()},
                'tasks':{task:{split:dict(n=100,precision=.5,recall=.5,auc=.6) for split in windows} for task in manifest['models']}})
            approval = dict(schema='PANJI_LAB_DELIVERY_V1',manifest_sha256=sha,lab_revision='c'*40,approved_for_production=True,
                files={name:digest_path(root/name) for name in ('golden_cases.json','validation_report.json')})
            atomic_json(root/'delivery.json',approval)
            self.assertEqual(verify_golden(root,sha)['status'],'GOLDEN_VERIFIED')
            cases[0]['expected']['signals'][0]['score'] += .01
            atomic_json(root/'golden_cases.json',cases)
            approval['files']['golden_cases.json'] = digest_path(root/'golden_cases.json')
            atomic_json(root/'delivery.json',approval)
            with self.assertRaises(AssertionError):
                verify_golden(root,sha)

    def test_partitioned_failure_resumes_only_verified_exact_input_stock(self):
        import contextlib
        import io
        from types import SimpleNamespace
        from app.strategy_integration.partitioned_build import build
        args = SimpleNamespace(as_of=str(self.raw.datetime.iloc[-1].date()), chart_bars=90)
        runtime = self.bundle['fourteen_runtime']
        original = producer.make_snapshots
        calls = []
        def interrupt(frames, *a, **kw):
            calls.extend(frames)
            if len(calls) == 2:
                raise RuntimeError('injected interruption after first stock')
            return original(frames, *a, **kw)
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            kwargs = dict(out=Path(folder), frames={'SH:600000':self.raw,'SZ:000001':self.raw},
                pit_source=None, source_report={'source_sha256':'f'*64}, fmod=runtime.engine,
                bundle=self.bundle, facts=self.facts, bundle_path=str(PACKAGE),
                bundle_origin='SYNTHETIC_DIAGNOSTIC', assets={}, release_id='a'*64, windows={'windows':{}})
            with patch.object(producer,'make_snapshots',side_effect=interrupt):
                with self.assertRaisesRegex(RuntimeError,'injected interruption'):
                    build(args, **kwargs)
            self.assertEqual(calls, ['SH:600000','SZ:000001'])
            calls.clear()
            def resumed(frames, *a, **kw):
                calls.extend(frames)
                return original(frames, *a, **kw)
            with patch.object(producer,'make_snapshots',side_effect=resumed):
                result = build(args, **kwargs)
            self.assertEqual(calls, ['SZ:000001'])
            self.assertTrue(Path(result['output']).is_dir())
            self.assertEqual(list((Path(folder)/'.strategy-checkpoints').iterdir()), [])

    def test_partitioned_build_consumes_the_same_production_readers(self):
        import contextlib
        import io
        from types import SimpleNamespace
        from app.services import strategy_partitioned_snapshot as transport
        from app.services.strategy_candidate_snapshot import get_today, read_dataset
        from app.services.strategy_research_read_service import read_snapshot, evaluate
        from app.strategy_integration.partitioned_build import build
        from app.strategy_integration.release_store import _validate_batch_for_activation
        args = SimpleNamespace(as_of=str(self.raw.datetime.iloc[-1].date()), chart_bars=120)
        runtime = self.bundle['fourteen_runtime']
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            frames = {'SH:600000': self.raw, 'SZ:000001': self.raw}
            result = build(args, out=Path(folder), frames=frames, pit_source=None,
                source_report={'source_sha256': 'f'*64}, fmod=runtime.engine, bundle=self.bundle,
                facts=self.facts, bundle_path=str(PACKAGE), bundle_origin='SYNTHETIC_DIAGNOSTIC',
                assets={}, release_id='a'*64, windows={'windows': {}})
            root = Path(result['output'])
            _validate_batch_for_activation(root)
            manifest = transport.index(root/'candidate_snapshot.json')
            with patch.dict('os.environ', {'PANJI_STRATEGY_RELEASE_ROOT': '',
                    'PANJI_STRATEGY_CANDIDATE_SNAPSHOT': str(root/'candidate_snapshot.json'),
                    'STRATEGY_RESEARCH_SNAPSHOT': str(root/'evaluation_snapshot.json')}):
                index = read_dataset()
                self.assertEqual(list(index['stocks']), ['SH:600000', 'SZ:000001'])
                stock = get_stock(read_dataset(symbol='SH:600000'), 'SH:600000')
                self.assertEqual(len(stock['bars']), 120)
                self.assertEqual(get_today(read_dataset(trade_date=args.as_of), args.as_of)['trade_date'], args.as_of)
                evaluation = read_snapshot()
                summary = evaluate_tasks(evaluation, period='UNASSIGNED')
                _, expected = producer.make_snapshots(frames, runtime.engine, self.bundle,
                    as_of=args.as_of, source_sha='f'*64, chart_bars=120, release_id='a'*64,
                    bundle_sha256=self.facts['bundle_sha256'])
                expected['metadata'] = manifest['evaluation_metadata']
                self.assertEqual(summary, evaluate_tasks(expected, period='UNASSIGNED'))
                self.assertEqual(evaluate(evaluation, 'BUY', None, 5, 3), evaluate(expected, 'BUY', None, 5, 3))
            first = next(iter(manifest['stocks'].values()))['candidate']
            (root/first['file']).write_text('{}')
            with self.assertRaisesRegex(ValueError, 'PARTITION_FILE_MISSING_OR_CHANGED'):
                transport.validate_publication(root/'candidate_snapshot.json')


if __name__ == '__main__':
    unittest.main()
