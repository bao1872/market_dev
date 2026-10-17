"""Admin-only, explicitly gated model management and prediction job *requests*.

Requests create durable JSON queue records; HTTP handlers NEVER run ML or training.
A separate disabled-by-default worker owns execution after deployment approval.
No DB migrations or extra permissions are introduced.
"""
from __future__ import annotations
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
import hashlib
import json
import os
from pathlib import Path
import re
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from app.services.access_control_service import AccessContext, require_admin
from app.strategy_integration.release_store import activate_batch, resolve_release_batch
from app.strategy_integration import rolling_windows as rw

router=APIRouter(prefix='/v1/admin/strategy-research/zigzag', tags=['strategy-research-admin'])
SHA_RE=re.compile(r'^[0-9a-f]{64}$')
SYMBOL_RE=re.compile(r'^(SH|SZ|BJ):[0-9]{6}$')


def _require_write_enabled():
    if os.getenv('PANJI_STRATEGY_ADMIN_WRITES_ENABLED')!='1':
        raise HTTPException(status_code=503,detail='策略管理仍处于部署前锁定状态')


def _job_root() -> Path:
    root=os.getenv('PANJI_STRATEGY_JOB_ROOT','')
    if not root or not Path(root).is_absolute():
        raise HTTPException(status_code=503,detail='尚未配置受控策略任务根目录')
    return Path(root)


def _train_enqueue_ready() -> bool:
    from app.strategy_integration.runtime_status import read_runtime_status
    root = os.getenv('PANJI_STRATEGY_JOB_ROOT', '')
    return bool(root and os.getenv('PANJI_STRATEGY_ADMIN_WRITES_ENABLED') == '1' and os.getenv('PANJI_STRATEGY_TRAIN_RUNTIME_READY') == '1' and read_runtime_status(root).get('can_train'))


def _save_job(kind:str,payload:dict[str,Any], *, retry_of=None) -> dict[str,Any]:
    _require_write_enabled()
    if kind == 'TRAIN' and not _train_enqueue_ready():
        raise HTTPException(status_code=503,detail='独立训练运行时尚未核验，禁止创建训练任务')
    if kind in ('PREDICT', 'EVALUATE'):
        from app.strategy_integration.runtime_status import read_runtime_status
        capability = 'can_predict' if kind == 'PREDICT' else 'can_train'
        if not read_runtime_status(_job_root()).get(capability):
            raise HTTPException(status_code=503, detail='策略执行运行时或模型尚未就绪')
    from app.strategy_integration.job_store import enqueue
    try:
        return enqueue(_job_root(), kind, payload, retry_of=retry_of)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc



class DateRange(BaseModel):
    start: date
    end: date

    @model_validator(mode='after')
    def ordered(self):
        if self.start>self.end:
            raise ValueError('start after end')
        return self


class TrainWindow(BaseModel):
    as_of:date
    train:DateRange
    val:DateRange
    test:DateRange
    trees:int=Field(300,ge=50,le=2000)
    model_family: Literal['fourteen'] = 'fourteen'

    @model_validator(mode='after')
    def no_overlap(self):
        if self.train.end>=self.val.start or self.val.end>=self.test.start:
            raise ValueError('train/val/test windows overlap')
        expected = rw.rolling_windows(self.as_of)['windows']
        for name in ('train','val','test'):
            supplied = getattr(self,name)
            if (supplied.start.isoformat(),supplied.end.isoformat()) != tuple(expected[name]):
                raise ValueError('TRAIN_WINDOW_NOT_ROLLING_24_6_3')
        return self


class PredictionRequest(BaseModel):
    trade_date:date


class PublishRequest(BaseModel):
    release_id:str=Field(min_length=64,max_length=64)
    candidate_report_sha256:str=Field(min_length=64,max_length=64)


@router.get('/train/windows')
def training_windows(ctx:AccessContext=Depends(require_admin)) -> dict:
    today=datetime.now(ZoneInfo('Asia/Shanghai')).date()
    w=rw.rolling_windows(today)
    return {
        'as_of':today.isoformat(),
        'train':{'start':w['windows']['train'][0],'end':w['windows']['train'][1]},
        'val':{'start':w['windows']['val'][0],'end':w['windows']['val'][1]},
        'test':{'start':w['windows']['test'][0],'end':w['windows']['test'][1]},
        'trees':300,
        'model_family':'fourteen',
        'model_count':14,
        'global_only':True,
        'status':'AUTHORITATIVE_ROLLING_24_6_3',
        'train_enqueue_enabled': _train_enqueue_ready(),
    }


@router.post('/train/preview')
def preview_training(request:TrainWindow,ctx:AccessContext=Depends(require_admin)) -> dict:
    return {'valid':False,'split_by':'candidate_d1_decision_date',
            'maturity_checked_at':None, 'label_contract':{'L':'HH','H':'LL'},
            'status':'SCHEMA_PREVIEW_ONLY',
            'message':'仅完成日期合同预检；样本成熟度/数量/PIT 尚未检查，必须由隔离训练链验证'}


@router.post('/train-runs',status_code=202)
def request_training(request:TrainWindow,ctx:AccessContext=Depends(require_admin)) -> dict:
    return _save_job('TRAIN',request.model_dump(mode='json'))


@router.post('/prediction-runs',status_code=202)
def request_prediction(request:PredictionRequest,ctx:AccessContext=Depends(require_admin)) -> dict:
    return _save_job('PREDICT',request.model_dump(mode='json'))


@router.get('/train-runs/{job_id}')
def job_status(job_id:str,ctx:AccessContext=Depends(require_admin)) -> dict:
    if not re.fullmatch('[0-9a-f]{32}',job_id):raise HTTPException(status_code=422,detail='invalid job')
    from app.strategy_integration.job_store import read_job
    job = read_job(_job_root(), job_id)
    if job is None:
        raise HTTPException(status_code=404,detail='job not found')
    return job


@router.get('/jobs')
def jobs(ctx:AccessContext=Depends(require_admin)) -> dict:
    from app.strategy_integration.job_store import list_jobs
    root = os.getenv('PANJI_STRATEGY_JOB_ROOT', '')
    if not root:
        return {'status': 'NOT_CONFIGURED', 'items': []}
    return {'status': 'AVAILABLE', 'items': list_jobs(Path(root))}


@router.post('/jobs/{job_id}/retry', status_code=202)
def retry_job(job_id: str, ctx:AccessContext=Depends(require_admin)) -> dict:
    from app.strategy_integration.job_store import read_job
    try:
        job = read_job(_job_root(), job_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not job or job['status'] != 'FAILED':
        raise HTTPException(status_code=409, detail='仅失败任务可以重新入队')
    return _save_job(job['kind'], job['payload'], retry_of=job_id)



@router.get('/model-releases')
def model_releases(ctx:AccessContext=Depends(require_admin)) -> dict:
    base=os.getenv('PANJI_STRATEGY_RELEASE_ROOT','')
    if not base or not Path(base).is_absolute():
        return {'releases':[],'active_release':None,'status':'NOT_CONFIGURED'}
    root=Path(base)
    records=[]
    seen = set()
    candidates = list(root.glob('*/manifest.json')) + list(root.glob('releases/*/manifest.json'))
    for p in sorted(candidates, key=lambda p: p.stat().st_mtime_ns, reverse=True)[:50]:
        m = json.loads(p.read_text())
        if SHA_RE.fullmatch(m.get('release_id', '')) and m['release_id'] not in seen:
            records.append({k:m.get(k) for k in ('release_id','label_contract','status','as_of','evaluation_sha256','validation')})
            seen.add(m['release_id'])
    active=root/'active_release.json'
    return {'releases':records,'active_release':json.loads(active.read_text()) if active.is_file() else None}


@router.get('/model-releases/personal/{symbol}')
def personal_model_release(symbol:str,ctx:AccessContext=Depends(require_admin)) -> dict:
    """Read one symbol's personal model record (admin-only, read-only).

    Exposes the per-symbol training state so an operator can see which symbols
    have an active personal model, which fell back, and why.
    """
    if not SYMBOL_RE.fullmatch(symbol):
        raise HTTPException(status_code=422,detail='invalid symbol')
    root=os.getenv('PANJI_STRATEGY_PERSONAL_REGISTRY','')
    if not root or not Path(root).is_absolute():
        return {'symbol':symbol,'status':'NOT_CONFIGURED'}
    from app.strategy_integration import personal_model as pm
    registry=pm.load_registry(Path(root))
    record=pm.resolve_active(registry,symbol)
    if record is None:
        candidate=registry['symbols'].get(symbol)
        if candidate is None:
            return {'symbol':symbol,'status':'NOT_TRAINED'}
        return {'symbol':symbol,'status':'NOT_ACTIVE',
                'mode':candidate.get('mode'),
                'fallback_reason':candidate.get('fallback_reason'),
                'model_version':candidate.get('model_version')}
    return {'symbol':symbol,'status':'ACTIVE',
            'mode':record.get('mode'),
            'fallback_reason':record.get('fallback_reason'),
            'model_version':record.get('model_version'),
            'release_id':record.get('release_id'),
            'global_release_id':record.get('global_release_id'),
            'price_input_contract':record.get('price_input_contract'),
            'windows':record.get('windows'),
            'samples':record.get('samples'),
            'rollback_parent':record.get('rollback_parent'),
            'artifact_path':record.get('artifact_path')}


@router.post('/model-releases/{release_id}/publish')
def publish_release(release_id:str,request:PublishRequest,ctx:AccessContext=Depends(require_admin)) -> dict:
    _require_write_enabled()
    if request.release_id!=release_id or not SHA_RE.fullmatch(release_id):
        raise HTTPException(status_code=422,detail='invalid release identity')
    root=os.getenv('PANJI_STRATEGY_RELEASE_ROOT','')
    if not root or not Path(root).is_absolute():
        raise HTTPException(status_code=503,detail='release root not configured')
    # Resolve the immutable batch: operator layout keeps it under
    # releases/<release_id>/, while a freshly built batch is named
    # <as_of>_<release_id[:16]>. Either way, activate_batch points the snapshot
    # pointer + the model record at the SAME batch, atomically.
    batch_dir = Path(root)/'releases'/release_id
    if not (batch_dir/'manifest.json').is_file():
        batch_dir = resolve_release_batch(Path(root), release_id)
    if batch_dir is None or not (batch_dir/'manifest.json').is_file():
        raise HTTPException(status_code=404,detail='candidate release unavailable')
    manifest = json.loads((batch_dir/'manifest.json').read_text())
    if manifest.get('status')!='AUDITED' or manifest.get('release_id')!=release_id or manifest.get('evaluation_sha256')!=request.candidate_report_sha256:
        raise HTTPException(status_code=409,detail='independent evaluation audit prerequisite missing')
    # One atomic pointer swap + complete, file-verified active release record.
    # This is the exact path the worker uses, so the admin publish can never leave
    # the model pointer (bundle_path/bundle_sha256) and the snapshot pointer
    # (current symlink) inconsistent, even if interrupted.
    activate_batch(Path(root), batch_dir)
    return {'status':'RELEASE_ACTIVATED','release_id':release_id}


@router.post('/model-releases/{release_id}/activate')
def activate_release(release_id:str,request:PublishRequest,ctx:AccessContext=Depends(require_admin)) -> dict:
    # Rollback uses same audit prerequisites as first activation.
    return publish_release(release_id,request,ctx)


@router.get('/runtime')
def runtime_status(ctx:AccessContext=Depends(require_admin)) -> dict:
    from app.strategy_integration.runtime_status import read_runtime_status
    root = os.getenv('PANJI_STRATEGY_JOB_ROOT', '')
    return read_runtime_status(root) if root else {'status': 'NOT_CONFIGURED', 'can_train': False, 'can_predict': False}


@router.post('/train-runs/{job_id}/evaluate', status_code=202)
def evaluate_training(job_id: str, request: PredictionRequest, ctx:AccessContext=Depends(require_admin)) -> dict:
    from app.strategy_integration.job_store import read_job
    try:
        job = read_job(_job_root(), job_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not job or job['status'] != 'SUCCEEDED' or job['kind'] != 'TRAIN':
        raise HTTPException(status_code=409, detail='训练候选尚未完成')
    return _save_job('EVALUATE', {'training_job_id': job_id, 'trade_date': request.trade_date.isoformat()})


@router.get('/train-runs/{job_id}/report')
def training_report(job_id: str, ctx:AccessContext=Depends(require_admin)) -> dict:
    from app.strategy_integration.job_store import read_job
    try:
        job = read_job(_job_root(), job_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not job or job['status'] != 'SUCCEEDED' or job['kind'] != 'TRAIN':
        raise HTTPException(status_code=409, detail='训练报告尚不可用')
    path = _job_root()/'outputs'/job_id/'train_report.json'
    if not path.is_file() or path.stat().st_size > 8*1024*1024:
        raise HTTPException(status_code=503, detail='训练报告缺失或超出读取上限')
    return json.loads(path.read_text())


@router.post('/model-releases/{release_id}/audit')
def audit_release(release_id: str, request: PublishRequest, ctx:AccessContext=Depends(require_admin)) -> dict:
    _require_write_enabled()
    if release_id != request.release_id or not SHA_RE.fullmatch(release_id):
        raise HTTPException(status_code=422, detail='invalid release identity')
    root = Path(os.getenv('PANJI_STRATEGY_RELEASE_ROOT', ''))
    if not root.is_absolute():
        raise HTTPException(status_code=503, detail='release root not configured')
    batch = resolve_release_batch(root, release_id)
    if batch is None:
        raise HTTPException(status_code=404, detail='candidate release unavailable')
    from app.strategy_integration.release_store import _validate_batch_for_activation, atomic_json, digest_path, PublishError
    manifest = json.loads((batch/'manifest.json').read_text())
    if manifest.get('status') not in ('VALIDATED_STAGING_NOT_DEPLOYED', 'AUDITED') or request.candidate_report_sha256 != manifest.get('evaluation_sha256'):
        raise HTTPException(status_code=409, detail='评价资产身份或校验状态不符')
    if digest_path(batch/'evaluation_snapshot.json') != request.candidate_report_sha256:
        raise HTTPException(status_code=409, detail='评价资产已变更')
    try:
        _validate_batch_for_activation(batch)
    except PublishError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    manifest.update(status='AUDITED', manual_review={'actor': str(ctx.user_id), 'at': datetime.now(timezone.utc).isoformat(),
                    'evaluation_sha256': request.candidate_report_sha256})
    atomic_json(batch/'manifest.json', manifest)
    return {'status':'AUDITED','release_id':release_id,'activated':False}
