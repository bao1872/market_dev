"""Operator-enabled durable job worker and optional isolated after-close loop.

Not called from FastAPI startup, existing DSA scheduler or process import.
This may run only after production deploy authorization and readiness checks.
"""
from __future__ import annotations
import argparse
from datetime import datetime, time
import json
import os
from pathlib import Path
import subprocess
import sys
import time as system_time
from zoneinfo import ZoneInfo

from .release_store import atomic_json, activate_batch


def enabled() -> bool:
    return os.getenv('PANJI_STRATEGY_WORKER_ENABLED') == '1'


def _roots() -> tuple[Path,Path,Path,Path]:
    if not enabled():raise RuntimeError('WORKER_DISABLED_PENDING_DEPLOY_APPROVAL')
    r=os.getenv('PANJI_STRATEGY_JOB_ROOT','')
    o=os.getenv('PANJI_STRATEGY_SNAPSHOT_ROOT','')
    bars=os.getenv('PANJI_STRATEGY_BARS_FILE','')
    inst=os.getenv('PANJI_STRATEGY_INSTRUMENTS_FILE','')
    if not all(x and Path(x).is_absolute() for x in (r,o,bars,inst)):
        raise RuntimeError('WORKER_PATHS_NOT_CONFIGURED')
    return Path(r),Path(o),Path(bars),Path(inst)


def process_one() -> dict:
    jobs,snap_root,bars,inst=_roots()
    (jobs/'queue').mkdir(parents=True,exist_ok=True)
    (jobs/'running').mkdir(parents=True,exist_ok=True)
    for req in sorted((jobs/'queue').glob('*.json')):
        running=jobs/'running'/req.name
        try:os.replace(req,running)
        except FileNotFoundError:continue
        job=json.loads(running.read_text())
        if job.get('status')!='QUEUED':raise RuntimeError('INVALID_JOB_STATE')
        job['status']='RUNNING';job['started_at']=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
        atomic_json(running,job)
        payload=job['payload'];k=job['kind'];job_dir=jobs/'outputs'/job['job_id']
        if job_dir.exists():raise RuntimeError('JOB_OUTPUT_DIR_EXISTS')
        runtime=os.getenv('PANJI_STRATEGY_PYTHON',sys.executable)
        if k=='PREDICT':
            # Production entrance: authoritative QFQ daily bars exported read-only from
            # the production DB through MarketDataAggregationService.get_bars_batch.
            # The offline diagnostic parquet is never used as an automatic fallback.
            export_dir=jobs/'exports'/job['job_id']
            cmd=[runtime,'-m','app.strategy_integration.cli','build',
                 '--source-mode','canonical',
                 '--bars',str(bars),'--instruments',str(inst),
                 '--as-of',payload['trade_date'],
                 '--export-dir',str(export_dir),
                 '--output-dir',str(snap_root)]
        elif k=='TRAIN':
            cfg=payload
            cmd=[runtime,'-m','app.strategy_integration.training','--bars',str(bars),'--instruments',str(inst),
                 '--output-dir',str(job_dir),
                 '--train-start',cfg['train']['start'],'--train-end',cfg['train']['end'],
                 '--val-start',cfg['val']['start'],'--val-end',cfg['val']['end'],
                 '--test-start',cfg['test']['start'],'--test-end',cfg['test']['end'],
                 '--trees',str(cfg['trees'])]
        else:raise RuntimeError('UNKNOWN_JOB_KIND')
        try:
            completed=subprocess.run(cmd,capture_output=True,text=True,timeout=10800,check=False,
                                     cwd=os.environ.get('PANJI_STRATEGY_CODE_ROOT') or None)
            job.update({'status':'SUCCEEDED' if completed.returncode==0 else 'FAILED',
                        'return_code':completed.returncode,
                        'stdout_tail':completed.stdout[-6000:],'stderr_tail':completed.stderr[-6000:]})
            # No auto promotion by default. Only optional, deployment-time switch.
            if k=='PREDICT' and completed.returncode==0 and os.getenv('PANJI_STRATEGY_AUTOPUBLISH_ENABLED')=='1':
                last_line = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else '{}'
                result=json.loads(last_line)
                release_root=Path(result.get('output',''))
                if not release_root.is_dir():raise RuntimeError('NO_VALIDATED_SNAPSHOT_OUTPUT')
                activate_batch(snap_root,release_root)
                job['snapshot_pointer_activated']=True
        except (subprocess.TimeoutExpired,Exception) as ex:
            job.update({'status':'FAILED','error':repr(ex)[:1000]})
        job['ended_at']=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
        folder=jobs/('finished' if job['status']=='SUCCEEDED' else 'failed')
        folder.mkdir(exist_ok=True,parents=True)
        atomic_json(folder/req.name,job)
        running.unlink(missing_ok=True)
        return {'job_id':job['job_id'],'status':job['status']}
    return {'status':'NO_QUEUED_JOBS'}


def run_daily_loop():
    """Only a separately supervised worker may poll; no HTTP/lifespan scheduling."""
    jobs,root,bars,inst=_roots()
    if os.getenv('PANJI_STRATEGY_DAILY_SCHEDULER_ENABLED')!='1':
        raise RuntimeError('DAILY_SCHEDULER_DISABLED')
    while True:
        now=datetime.now(ZoneInfo('Asia/Shanghai'))
        # Input freshness and coverage are independently checked by build.
        if now.time()>=time(18,30) and now.weekday()<5:
            day=now.date().isoformat()
            identifier=jobs/'daily_intents'/f'{day}.json'
            if not identifier.exists():
                identifier.parent.mkdir(parents=True,exist_ok=True)
                # Idempotent create. Source readiness is verified by exporter.
                try:
                    with identifier.open('x') as f:
                        json.dump({'as_of':day},f)
                    job={'job_id':__import__('uuid').uuid4().hex,'kind':'PREDICT','status':'QUEUED',
                         'created_at':now.isoformat(),'payload':{'trade_date':day}}
                    (jobs/'queue').mkdir(parents=True,exist_ok=True)
                    atomic_json(jobs/'queue'/(job['job_id']+'.json'),job)
                except FileExistsError:pass
        process_one()
        system_time.sleep(900)


def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['once','daily-loop'])
    args=p.parse_args()
    if args.command=='once':print(json.dumps(process_one()))
    else:run_daily_loop()

if __name__=='__main__':main()
