"""Causal, portable inference for the 14 research tasks. No training/oracle labels."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import numpy as np
import pandas as pd

class FourteenModels:
    def __init__(self, package_dir=None):
        from lightgbm import Booster
        self.root=Path(package_dir) if package_dir else Path(__file__).resolve().parent
        self.manifest=json.loads((self.root/'manifest.json').read_text())
        for name, expected in self.manifest['files'].items():
            if hashlib.sha256((self.root/name).read_bytes()).hexdigest()!=expected:
                raise ValueError(f'PACKAGE_HASH_MISMATCH: {name}')
        spec=importlib.util.spec_from_file_location('panji_fourteen_frozen_engine',self.root/'feature_engine.py')
        self.engine=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.engine)
        if list(self.engine.ALL_COLS)!=self.manifest['feature_columns']:
            raise ValueError('FEATURE_SCHEMA_MISMATCH')
        self.models={k:Booster(model_file=str(self.root/v['file'])) for k,v in self.manifest['models'].items()}
        if len(self.models)!=14:raise ValueError('EXPECTED_14_MODELS')
        if any(m.feature_name()!=self.manifest['feature_columns'] for m in self.models.values()):
            raise ValueError('MODEL_FEATURE_SCHEMA_MISMATCH')

    @staticmethod
    def prepare(raw, as_of=None):
        required=['datetime','open','high','low','close','volume']
        d=raw[required].copy();d['datetime']=pd.to_datetime(d.datetime)
        if d.datetime.isna().any() or d.datetime.duplicated().any():raise ValueError('INVALID_DATES')
        d=d.sort_values('datetime').reset_index(drop=True)
        if as_of is not None:
            cutoff=pd.Timestamp(as_of)
            if cutoff!=cutoff.normalize():raise ValueError('AS_OF_MUST_BE_A_DATE: daily-close inference only')
            d=d[d.datetime.dt.normalize()<=cutoff].reset_index(drop=True)
        d[required[1:]]=d[required[1:]].apply(pd.to_numeric,errors='raise')
        a=d[required[1:]].to_numpy(float)
        if not (np.isfinite(a).all() and (a[:,:4]>0).all() and (a[:,4]>=0).all()):raise ValueError('INVALID_OHLCV')
        if not ((d.high>=d[['open','low','close']].max(axis=1)).all() and
                (d.low<=d[['open','high','close']].min(axis=1)).all()):raise ValueError('INVALID_CANDLE')
        return d

    def score(self, raw, as_of=None):
        """Return all eligible tasks at last available bar, with no future selection.

        The caller supplies consistently adjusted daily OHLC and raw volume. Bar indices
        refer to this input history, not global exchange indices. as_of is inclusive by day.
        """
        d=self.prepare(raw,as_of)
        answer=dict(schema=self.manifest['schema'],model_version=self.manifest['version'],
                    requested_as_of=str(as_of) if as_of is not None else None,
                    decision_dt=None if d.empty else d.datetime.iloc[-1].isoformat(),
                    status='INSUFFICIENT_HISTORY',uncalibrated=True,research_data_end=self.manifest['data_end'],signals=[])
        if len(d)<21:return answer
        t=len(d)-1
        # Only supplied prefix is replayed. Final here means confirmed AS OF this prefix.
        _,snap,_=self.engine.replay(d)
        feature_rows,X=self.engine.features(d,snap,[t])
        candidates,_=self.engine.causal_candidates(d)
        H=d.high.to_numpy(float);L=d.low.to_numpy(float)
        active=[]
        for j,kind,price in candidates:
            stage=t-j
            if stage not in (1,2,3):continue
            segment=H[j+1:t+1] if kind=='H' else L[j+1:t+1]
            superseded=(segment>price).any() if kind=='H' else (segment<price).any()
            if superseded:continue
            active.append((j,kind,stage))
        def emit(layer,kind,stage,j):
            key=f'{layer}_{kind}_D{stage}';meta=self.manifest['models'][key]
            score=float(self.models[key].predict(X,num_threads=1)[0])
            answer['signals'].append(dict(task=key,layer=layer,kind=kind,stage=stage,
                anchor_dt=d.datetime.iloc[j].isoformat(),anchor_bar=int(j),decision_bar=int(t),
                score=score,threshold=meta['threshold'],above_threshold=bool(score>=meta['threshold']),
                target=meta['target'],condition=meta['condition'],
                evaluation_domain_verified=(layer!='L2')))
        for j,kind,stage in active:
            emit('L1',kind,stage,j)
            # Score on every causal candidate; do not require an oracle-confirmed anchor.
            emit('L2',kind,stage,j)
        ph=H[t]>max(H[t-5:t]);pl=L[t]<min(L[t-5:t])
        if ph != pl:
            kind='H' if ph else 'L'
            if not any(k==kind for j,k,stage in active):emit('D0',kind,0,t)
        answer.update(status='SCORED' if answer['signals'] else 'NO_ELIGIBLE_TASK',
                      structural_features_available=bool(feature_rows.signal_available.iloc[0]))
        return answer

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',required=True)
    p.add_argument('--as-of');p.add_argument('--output');args=p.parse_args()
    path=Path(args.input)
    raw=pd.read_parquet(path) if path.suffix=='.parquet' else pd.read_csv(path)
    result=FourteenModels().score(raw,args.as_of)
    text=json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)
    if args.output:Path(args.output).write_text(text+'\n')
    else:print(text)
if __name__=='__main__':main()
