"""Configurable, leakage-gated H/L × D1-D3 × L1/L2 training.

Outputs immutable *candidate* model artifacts. NEVER changes active release,
never schedules itself, never updates the original supplied pickle.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from datetime import date
import hashlib
import json
from pathlib import Path
import pickle
import sys

import numpy as np
import pandas as pd

from . import model, source
from .producer import ProducerError

# NOTE: intentionally separate from original research train() because that
# function does not enforce label maturity across *all* split boundaries.


def _window_from_args(args):
    windows = []
    for name in ('train','val','test'):
        lo, hi = date.fromisoformat(getattr(args,name+'_start')), date.fromisoformat(getattr(args,name+'_end'))
        if lo > hi: raise ProducerError('REVERSED_'+name.upper())
        windows.append((name,lo,hi))
    if not (windows[0][2] < windows[1][1] and windows[1][2] < windows[2][1]):
        raise ProducerError('TRAIN_VAL_TEST_OVERLAP')
    return windows


def _labels(raw, final, j, kind, cutoff_bar, stage_bar):
    key = next((i for i,p in enumerate(final) if p['bar']==j and p['type']==kind),None)
    if key is not None and final[key]['confirmed_at']<=cutoff_bar:
        y1 = 1
    elif key is None and any(p['bar']>j and p['type']!=kind and p['confirmed_at']<=cutoff_bar for p in final):
        y1 = 0
    else:
        y1 = None
    y2 = None
    if key is not None and key>0 and key+1<len(final):
        prev, nxt = final[key-1], final[key+1]
        if (prev['type']==nxt['type'] and nxt['confirmed_at']<=cutoff_bar
            and nxt['bar']>j+3 and nxt['bar']>stage_bar):
            y2 = int(float(nxt['price']) > float(prev['price'])) if kind=='L' else int(float(nxt['price']) < float(prev['price']))
    return y1,y2


def build_dataset(stock_raw, fmod, windows):
    values = defaultdict(list)
    counters = Counter()
    for symbol,raw in stock_raw.items():
        final,snap,_=fmod.replay(raw)
        candidates,_=fmod.causal_candidates(raw)
        days=[pd.Timestamp(x).date() for x in raw.datetime]
        split_end_bars={name:next((i for i in range(len(days)-1,-1,-1) if days[i] <= hi),-1)
                        for name,lo,hi in windows}
        stage_events=[]
        for j,kind,_px in candidates:
            if j+3 >=len(raw): continue
            d1,d3=days[j+1],days[j+3]
            group=next(((name,hi) for name,lo,hi in windows if lo<=d1 and d3<=hi),None)
            if group is None:
                counters['boundary_dropped']+=1;continue
            name,_hi=group
            cutoff=split_end_bars[name]
            for k in (1,2,3):
                t=j+k
                l1,l2=_labels(raw,final,j,kind,cutoff,t)
                if l1 is None and l2 is None:
                    counters['unresolved_both']+=1;continue
                stage_events.append((j,kind,k,t,name,l1,l2))
        if not stage_events:continue
        bars=sorted(set(s[3] for s in stage_events))
        _,matrix=fmod.features(raw,snap,bars)
        look={bar:matrix[i] for i,bar in enumerate(bars)}
        for j,kind,k,t,name,l1,l2 in stage_events:
            for layer,label in (('L1',l1),('L2',l2)):
                if label is None:continue
                values[(name,layer,kind,k)].append((look[t],label,f'{symbol}|{days[j]}|{kind}'))
                counters[f'{name}_{layer}_{kind}_D{k}']+=1
    return values,dict(counters)


def train(args):
    from lightgbm import LGBMClassifier
    from sklearn.metrics import roc_curve, roc_auc_score
    windows=_window_from_args(args)
    if args.max_symbols and not args.allow_partial_diagnostics:
        raise ProducerError('PARTIAL_TRAIN_REQUIRES_EXPLICIT_DIAGNOSTIC_FLAG')
    assets=model.validate_source_assets(args.assets)
    fmod=model.load_engine(args.assets)
    stock_raw,src=source.load_official_parquet(args.bars,args.instruments,args.test_end,max_symbols=args.max_symbols)
    data,counts=build_dataset(stock_raw,fmod,windows)
    model_map={};thresholds={};metrics={}
    for layer in ('L1','L2'):
        for kind in ('H','L'):
            for k in (1,2,3):
                key=(layer,kind,k)
                tr=data.get(('train',*key),[])
                va=data.get(('val',*key),[])
                te=data.get(('test',*key),[])
                if min(len(tr),len(va),len(te))<args.min_samples:
                    raise ProducerError(f'INSUFFICIENT_SPLIT_SAMPLE_{key}_{len(tr)}_{len(va)}_{len(te)}')
                def unpack(rows):
                    return np.stack([r[0] for r in rows]).astype(np.float32),np.asarray([r[1] for r in rows],dtype=int)
                xt,yt=unpack(tr);xv,yv=unpack(va);xe,ye=unpack(te)
                if min(np.bincount(yt,minlength=2))<args.min_each_class or min(np.bincount(yv,minlength=2))<args.min_each_class:
                    raise ProducerError(f'INSUFFICIENT_CLASS_{key}')
                clf=LGBMClassifier(n_estimators=args.trees,num_leaves=31,learning_rate=.045,
                    min_child_samples=80,max_depth=-1,reg_lambda=5.0,verbosity=-1,
                    n_jobs=args.threads,random_state=20261013)
                clf.fit(pd.DataFrame(xt,columns=fmod.ALL_COLS),yt)
                val=clf.predict_proba(pd.DataFrame(xv,columns=fmod.ALL_COLS))[:,1]
                fpr,tpr,thr=roc_curve(yv,val)
                ix=int(np.argmax(tpr-fpr));tau=float(thr[ix])
                if not np.isfinite(tau) or tau<0 or tau>1:
                    raise ProducerError('INVALID_VAL_THRESHOLD')
                model_map[key]=clf;thresholds[key]=tau
                for name,X,y in [('TRAIN',xt,yt),('VAL',xv,yv),('TEST',xe,ye)]:
                    score=clf.predict_proba(pd.DataFrame(X,columns=fmod.ALL_COLS))[:,1]
                    pred=score>=tau
                    tp=int(((pred==1)&(y==1)).sum());tn=int(((pred==0)&(y==0)).sum())
                    fp=int(((pred==1)&(y==0)).sum());fn=int(((pred==0)&(y==1)).sum())
                    metrics[f'{layer}:{kind}:D{k}:{name}']={'n':len(y),'positive':tp+fn,'tp':tp,'fp':fp,'tn':tn,'fn':fn,
                         'auc':float(roc_auc_score(y,score)) if len(np.unique(y))==2 else None}
    bundle={'schema':'PANJI_TWO_STAGE_FINAL_D123_V1','version':'panji-two-stage-final-d123-1.0',
            'feature_sha':model.ENGINE_SHA,'feature_dim':131,'layers':['L1','L2'],
            'kinds':['H','L'],'stages':[1,2,3],'models':model_map,'thresholds':thresholds,
            'uncalibrated':True,'target':{'L':'HH','H':'LL'},
            'label_contract':'L_HH__H_LL__V1','split_windows':[(n,str(lo),str(hi)) for n,lo,hi in windows]}
    output=args.output_dir.resolve()
    if output.exists() and list(output.iterdir()):raise ProducerError('CANDIDATE_MODEL_DIR_NOT_EMPTY')
    output.mkdir(parents=True,exist_ok=True)
    file=output/'two_stage_models_candidate_UNPUBLISHED.pkl'
    with file.open('xb') as f:
        pickle.dump(bundle,f,pickle.HIGHEST_PROTOCOL)
    sha=source.sha_file(file)
    (output/'train_report.json').write_text(json.dumps({'status':'CANDIDATE_TRAINED_NOT_PUBLISHED',
        'new_bundle_sha256':sha,'training_source_sha256':src['source_sha256'],
        'split_windows':bundle['split_windows'],'metrics':metrics,'counts':counts,
        'released':False,'active':False,'feature_sha':model.ENGINE_SHA},ensure_ascii=False,indent=2))
    print(json.dumps({'status':'CANDIDATE_TRAINED_NOT_PUBLISHED','sha256':sha,'dir':str(output)}))


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--assets',type=Path,default=model.ASSET_DIR)
    p.add_argument('--bars',type=Path,required=True);p.add_argument('--instruments',type=Path,required=True)
    p.add_argument('--output-dir',type=Path,required=True)
    for name in ('train','val','test'):
        for suffix in ('start','end'):p.add_argument('--'+name+'-'+suffix,required=True)
    p.add_argument('--min-samples',type=int,default=100)
    p.add_argument('--min-each-class',type=int,default=10)
    p.add_argument('--trees',type=int,default=300);p.add_argument('--threads',type=int,default=4)
    p.add_argument('--max-symbols',type=int,default=0)
    p.add_argument('--allow-partial-diagnostics',action='store_true')
    args=p.parse_args();train(args)

if __name__=='__main__':
    try:main()
    except (ProducerError,model.AssetError,ValueError) as exc:
        print('STOP_TRAIN',str(exc),file=sys.stderr);raise SystemExit(2)
