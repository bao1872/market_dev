"""Admin-only, explicitly gated model management and prediction job *requests*.

Requests create durable JSON queue records; HTTP handlers NEVER run ML or training.
A separate disabled-by-default worker owns execution after deployment approval.
No DB migrations or extra permissions are introduced.
"""
from __future__ import annotations
from datetime import date, datetime, timezone
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


def _save_job(kind:str,payload:dict[str,Any]) -> dict[str,Any]:
    _require_write_enabled()
    root=_job_root()/'queue'
    root.mkdir(parents=True,exist_ok=True)
    j={'job_id':uuid.uuid4().hex,'kind':kind,'status':'QUEUED',
       'created_at':datetime.now(timezone.utc).isoformat(),'payload':payload}
    path=root/(j['job_id']+'.json')
    with path.open('x',encoding='utf-8') as f:
        json.dump(j,f,ensure_ascii=False)
    return {'job_id':j['job_id'],'status':'QUEUED'}


class DateRange(BaseModel):
    start: date
    end: date

    @model_validator(mode='after')
    def ordered(self):
        if self.start>self.end:
            raise ValueError('start after end')
        return self


class TrainWindow(BaseModel):
    train:DateRange
    val:DateRange
    test:DateRange
    trees:int=Field(300,ge=50,le=2000)

    @model_validator(mode='after')
    def no_overlap(self):
        if self.train.end>=self.val.start or self.val.end>=self.test.start:
            raise ValueError('train/val/test windows overlap')
        return self


class PredictionRequest(BaseModel):
    trade_date:date


class PublishRequest(BaseModel):
    release_id:str=Field(min_length=64,max_length=64)
    candidate_report_sha256:str=Field(min_length=64,max_length=64)


@router.post('/train/preview')
def preview_training(request:TrainWindow,ctx:AccessContext=Depends(require_admin)) -> dict:
    return {'valid':True,'split_by':'candidate_d1_decision_date',
            'maturity_checked_at':'each_split_end', 'label_contract':{'L':'HH','H':'LL'},
            'status':'SCHEMA_PREVIEW_ONLY',
            'message':'完整样本成熟度/数量/PIT 检查由独立 worker 在排队执行前完成'}


@router.post('/train-runs',status_code=202)
def request_training(request:TrainWindow,ctx:AccessContext=Depends(require_admin)) -> dict:
    return _save_job('TRAIN',request.model_dump(mode='json'))


@router.post('/prediction-runs',status_code=202)
def request_prediction(request:PredictionRequest,ctx:AccessContext=Depends(require_admin)) -> dict:
    return _save_job('PREDICT',request.model_dump(mode='json'))


@router.get('/train-runs/{job_id}')
def job_status(job_id:str,ctx:AccessContext=Depends(require_admin)) -> dict:
    if not re.fullmatch('[0-9a-f]{32}',job_id):raise HTTPException(status_code=422,detail='invalid job')
    root=_job_root()
    for folder in ('queue','running','finished','failed'):
        p=root/folder/(job_id+'.json')
        if p.is_file():return json.loads(p.read_text())
    raise HTTPException(status_code=404,detail='job not found')


@router.get('/model-releases')
def model_releases(ctx:AccessContext=Depends(require_admin)) -> dict:
    base=os.getenv('PANJI_STRATEGY_RELEASE_ROOT','')
    if not base or not Path(base).is_absolute():
        return {'releases':[],'active_release':None,'status':'NOT_CONFIGURED'}
    root=Path(base)
    records=[]
    for p in sorted(root.glob('releases/*/manifest.json')):
        if SHA_RE.fullmatch(p.parent.name):
            m=json.loads(p.read_text())
            records.append({k:m.get(k) for k in ('release_id','bundle_sha256','label_contract','status','created_at')})
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
