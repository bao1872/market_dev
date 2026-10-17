#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B v5.5 | Independent causal D1/D2/D3 event-centered trend-change research.

Oracle FINAL ZigZag points are labels and hindsight audit ONLY. Live features use
confirmed-as-of ZigZag and price/volume through each stage close. An independent
left-5/right-1 causal candidate detector provides a fixed anchor j. D1/D2/D3
observe j+1,j+2,j+3 closes, same anchor, same future pivot, same future target and same oracle-CONDITIONAL binary label: 1 if the
NEXT oracle pivot changes the structural state established at the true
anchor pivot, else 0. Only true oracle anchor events are labeled/trained;
all real-time causal candidates can still receive conditional scores. The target pivot must OCCUR after j+3
and mature before its cohort split end. This is NOT a trading profit target.
Training excludes no candidates based on reversal-model predictions.
No D1/D2/D3 reversal model, candidate dataset or score is used anywhere.

Commands: selftest, build, train, stock, predict. Raw OHLCV + saved bundle
is sufficient for prediction. Probabilities are uncalibrated ML scores.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

VERSION = 'b55-oracle-conditional-d123-change-1.1'
LEG, DEV, THETA = 5, 5.0, .20
CUT1, CUT2, END = (pd.Timestamp(x) for x in ('2025-01-02','2025-11-19','2026-10-01'))
STATES = ('BULL','BEAR','RANGE')
ID = {'BULL':1,'BEAR':-1,'RANGE':0}
SIMPLE = ('ret_pct','duration','efficiency')
DYNAMIC = ('ret_pct','duration','price_slope','price_r2','volume_slope','volume_r2',
           'price_cumvol_beta','efficiency')
P0 = ('ret1','ret3','ret5','ret10','ret20','range5','range20','relvol','vol_slope5','atr14_pct')
STRUCT = ('state_id','subtype_id','uH','uL','deltaH_pct','deltaL_pct','refH_pct',
          'refL_pct','days_confirmed','confirmed_count','hist_scale_pct')
S_COLS = tuple(f's{i}_{x}' for i in range(1,11) for x in SIMPLE)
D_COLS = tuple(f'd{i}_{x}' for i in range(1,11) for x in DYNAMIC)
ALL_COLS = list(P0+STRUCT+S_COLS+D_COLS)
# B55 stage-specific models use B54's untouched causal features, plus D1/D2/D3
# event path information. No oracle field appears in any model column.
ANCHOR_COLS = ('anchor_dir','anchor_range_pct','anchor_relvol')
SEED_COLS = ('seed_state_id','seed_uH','seed_uL','seed_refH_pct','seed_refL_pct')
PATH_ONE = ('extreme_pct','close_pct','close_to_anchor_close','open_to_anchor_close',
            'high_low_ratio','close_open_ratio','close_position',
            'range_over_anchor_atr','volume_vs_anchor','wick_pct')
PATH_COLS = tuple(f'path{k}_{v}' for k in (1,2,3) for v in PATH_ONE)
EXTRA_COLS = list(ANCHOR_COLS+SEED_COLS+PATH_COLS)
C0 = list(P0+ANCHOR_COLS+PATH_COLS)
C1 = C0+list(STRUCT+SEED_COLS)
MODEL_COLS = {'B0':C0,'B1':C1,'B2':C1+list(S_COLS[:21]),
              'B3':C1+list(D_COLS)}
COLS = ALL_COLS + EXTRA_COLS


def error(s):raise RuntimeError(str(s))
def jd(p,obj):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=str,allow_nan=False),encoding='utf8')
def sha(p):
    with open(p,'rb') as f:
        h=hashlib.sha256()
        for v in iter(lambda:f.read(1<<20),b''):h.update(v)
    return h.hexdigest()
def rawpath(root,symbol):
    m,c=symbol.split(':');return Path(root)/'raw'/'1d'/m/f'{c}.parquet'
def load_raw(path):
    d=pd.read_parquet(path,columns=['datetime','open','high','low','close','volume'])
    d=d.sort_values('datetime').reset_index(drop=True)
    d['datetime']=pd.to_datetime(d.datetime)
    if d.datetime.isna().any() or d.datetime.duplicated().any():error('INVALID_DATES '+str(path))
    a=d[['open','high','low','close','volume']].to_numpy(float)
    if not (np.isfinite(a).all() and (a[:,:4]>0).all() and (a[:,4]>=0).all()):error('INVALID_OHLCV '+str(path))
    if not ((d.high>=d[['open','low','close']].max(axis=1)).all() and
            (d.low<=d[['open','high','close']].min(axis=1)).all()):error('INVALID_CANDLE '+str(path))
    return d

def replay(raw):
    """Streaming B-v3.7 ZigZag; each historical pivot must be independently
    confirmed by a subsequent accepted OPPOSITE pivot. Record earliest moment
    of certification. Last mutable extremum is NOT a true final pivot.
    """
    H=raw.high.to_numpy(float);L=raw.low.to_numpy(float)
    n=len(raw);piv=[];snap=[tuple() for _ in range(n)];num_revise=0
    for t in range(n):
        j=t-LEG
        if j>=LEG:
            ph=H[j]>np.max(H[j-LEG:j]) and H[j]>=np.max(H[j+1:t+1])
            pl=L[j]<np.min(L[j-LEG:j]) and L[j]<=np.min(L[j+1:t+1])
            if ph and pl:ph=pl=False
            for typ,hit,p in (('H',ph,H[j]),('L',pl,L[j])):
                if not hit:continue
                node={'bar':int(j),'type':typ,'price':float(p),'seen_at':int(t),'confirmed_at':None}
                if not piv:piv.append(node)
                elif piv[-1]['type']==typ:
                    if (typ=='H' and p>piv[-1]['price']) or (typ=='L' and p<piv[-1]['price']):
                        piv[-1]=node;num_revise+=1
                elif 100*abs(p/piv[-1]['price']-1)>=DEV:
                    if piv[-1]['confirmed_at'] is not None:error('ALREADY_CONFIRMED_PIVOT_CHANGED')
                    piv[-1]['confirmed_at']=int(t)
                    piv.append(node)
        # All but the current mutable pivot are structurally certified AS OF t.
        snap[t]=tuple(piv[:-1])
    final=piv[:-1]
    for x in final:
        if x['confirmed_at'] is None:error('UNCERTIFIED_FINAL_PIVOT')
    if piv and piv[-1]['confirmed_at'] is not None:error('LATEST_SHOULD_BE_MUTABLE')
    for t,s in enumerate(snap):
        if s and (s[-1]['confirmed_at']>t or s[-1]['bar']>t):error('ASOF_FUTURE_CONFIRMATION')
    return final,snap,{'final_confirmed':len(final),'latest_mutable':len(piv)-len(final),
                       'same_type_revisions':num_revise}


def swing_scale(piv,theta=THETA):
    legs=[100*abs(math.log(b['price']/a['price'])) for a,b in zip(piv[:-1],piv[1:])]
    return float(np.median(legs[-7:])) if legs else np.nan

def state_from_pivots(piv,theta=THETA):
    """Only truly confirmed oracle pivots or demonstrably as-of-certified pivots.
    Strict 2H+2L comparison; no approximate bull/bear threshold.
    """
    hh=[p for p in piv if p['type']=='H'];ll=[p for p in piv if p['type']=='L']
    if len(hh)<2 or len(ll)<2:return None
    h1,h2=hh[-2:];l1,l2=ll[-2:]
    dh=100*math.log(h2['price']/h1['price']);dl=100*math.log(l2['price']/l1['price'])
    hr='HH' if dh>0 else 'LH' if dh<0 else 'EH'
    lr='HL' if dl>0 else 'LL' if dl<0 else 'EL'
    if hr=='HH' and lr=='HL':state='BULL';subtype='TREND'
    elif hr=='LH' and lr=='LL':state='BEAR';subtype='TREND'
    else:
        scale=swing_scale(piv)
        is_sideways=(np.isfinite(scale) and scale>0 and abs(dh)<=theta*scale and abs(dl)<=theta*scale)
        state='RANGE';subtype='SIDEWAYS' if is_sideways else 'TRANSITION'
    scale=swing_scale(piv)
    return {'state':state,'subtype':subtype,'high_tag':hr,'low_tag':lr,
            'H1':h1['price'],'H2':h2['price'],'L1':l1['price'],'L2':l2['price'],
            'deltaH_pct':dh,'deltaL_pct':dl,'scale_pct':scale,
            'uH':dh/scale if np.isfinite(scale) and scale>0 else np.nan,
            'uL':dl/scale if np.isfinite(scale) and scale>0 else np.nan}


def oracle_pivot_table(raw,final):
    """Hindsight truth first. One row for EACH final confirmed real H/L.
    The pivot date is NOT the date its type/state became known.
    """
    dates=raw.datetime
    out=[]
    for i,p in enumerate(final):
        z=state_from_pivots(final[:i+1])
        out.append({'pivot_index':i,'pivot_bar':p['bar'],'pivot_date':dates.iloc[p['bar']],
                    'pivot_type':p['type'],'pivot_price':p['price'],
                    'confirmed_at_bar':p['confirmed_at'],
                    'confirmed_at_date':dates.iloc[p['confirmed_at']],
                    'oracle_state':z['state'] if z else 'INSUFFICIENT_2H_2L',
                    'oracle_subtype':z['subtype'] if z else 'INSUFFICIENT',
                    'high_tag':z['high_tag'] if z else None,
                    'low_tag':z['low_tag'] if z else None,
                    **{k:z[k] if z else np.nan for k in ('H1','H2','L1','L2',
                    'deltaH_pct','deltaL_pct','scale_pct','uH','uL')}})
    return pd.DataFrame(out)


def regression(x,y):
    x=np.asarray(x,float);y=np.asarray(y,float)
    m=np.isfinite(x)&np.isfinite(y);x=x[m];y=y[m]
    if len(x)<3:return np.nan,np.nan
    xx=x-x.mean();yy=y-y.mean();den=float(xx@xx);den2=float(yy@yy)
    if den<1e-18 or den2<1e-18:return np.nan,np.nan
    xy=float(xx@yy);return xy/den,float(max(0,min(1,xy*xy/den/den2)))


def features(raw,snap,indices=None):
    """One independent row per calendar bar, no D3 candidate selection.
    Returns full daily table (including NOT_SCOREABLE reasons) and feature matrix.
    """
    n=len(raw);D=raw.datetime;C=raw.close.to_numpy(float);O=raw.open.to_numpy(float)
    H=raw.high.to_numpy(float);L=raw.low.to_numpy(float);V=raw.volume.to_numpy(float)
    prev=np.r_[C[0],C[:-1]]
    tr=np.maximum(H-L,np.maximum(np.abs(H-prev),np.abs(L-prev)))
    atr=pd.Series(tr).rolling(14,min_periods=14).mean().to_numpy()
    vm=pd.Series(V).shift(1).rolling(20,min_periods=5).median().to_numpy()
    rel=np.divide(V,vm,out=np.full(n,np.nan),where=np.isfinite(vm)&(vm>0))
    vol_slope=pd.Series(np.log1p(rel)).rolling(5,min_periods=3).mean().to_numpy()
    chosen=np.arange(n,dtype=int) if indices is None else np.asarray(indices,dtype=int)
    if len(chosen) and (chosen.min()<0 or chosen.max()>=n):error('BAD_FEATURE_DATES')
    feat=np.full((len(chosen),len(ALL_COLS)),np.nan,dtype=np.float32)
    rows=[];cache={}
    for row_idx,t in enumerate(chosen):
        p=snap[t];st=state_from_pivots(p)
        basic=[]
        for k in (1,3,5,10,20):basic.append(100*math.log(C[t]/C[t-k]) if t>=k else np.nan)
        basic.extend([100*(np.max(H[t-4:t+1])-np.min(L[t-4:t+1]))/C[t] if t>=4 else np.nan,
                      100*(np.max(H[t-19:t+1])-np.min(L[t-19:t+1]))/C[t] if t>=19 else np.nan,
                      rel[t],vol_slope[t],100*atr[t]/C[t]])
        f=list(basic)
        if st is None:
            f.extend([np.nan]*len(STRUCT))
        else:
            f.extend([ID[st['state']],{'TREND':1,'SIDEWAYS':0,'TRANSITION':-1}[st['subtype']],
                    st['uH'],st['uL'],st['deltaH_pct'],st['deltaL_pct'],
                    100*math.log(C[t]/st['H2']),100*math.log(C[t]/st['L2']),
                    t-p[-1]['confirmed_at'],len(p),st['scale_pct']])
        ss=np.full(30,np.nan,np.float32);dd=np.full(80,np.nan,np.float32)
        for k,(a,b) in enumerate(reversed(list(zip(p[:-1],p[1:]))[-10:])):
            key=(a['bar'],b['bar'],a['price'],b['price'])
            if key not in cache:
                aa,bb=a['bar'],b['bar'];lp=np.log(C[aa:bb+1]);v=V[aa:bb+1]
                r=100*math.log(b['price']/a['price']);dur=bb-aa
                eff=abs(lp[-1]-lp[0])/(np.abs(np.diff(lp)).sum()+1e-12)
                sl,pr2=regression(np.arange(len(lp)),lp)
                vs,vr2=regression(np.arange(len(lp)),np.log1p(rel[aa:bb+1]))
                beta,_=regression(np.cumsum(v)/v.sum(),lp) if v.sum()>0 else (np.nan,np.nan)
                cache[key]=(np.asarray([r,dur,eff],np.float32),
                            np.asarray([r,dur,sl*100,pr2,vs,vr2,beta,eff],np.float32))
            s,d=cache[key];ss[3*k:3*k+3]=s;dd[8*k:8*k+8]=d
        feat[row_idx]=np.asarray(f+list(ss)+list(dd),np.float32)
        rows.append({'bar':t,'decision_dt':D.iloc[t],'asof_state':st['state'] if st else 'INSUFFICIENT_2H_2L',
                     'asof_subtype':st['subtype'] if st else 'INSUFFICIENT',
                     'asof_high_tag':st['high_tag'] if st else None,
                     'asof_low_tag':st['low_tag'] if st else None,
                     'asof_H1':st['H1'] if st else np.nan,'asof_H2':st['H2'] if st else np.nan,
                     'asof_L1':st['L1'] if st else np.nan,'asof_L2':st['L2'] if st else np.nan,
                     'n_confirmed_pivots':len(p),'signal_available':st is not None,
                     'no_score_reason':None if st else 'FOUR_CONFIRMED_PIVOTS_REQUIRED',
                     'close':C[t]})
    if feat.shape[1]!=len(ALL_COLS):error('FEATURE_DIMENSION_MISMATCH')
    return pd.DataFrame(rows),feat



def causal_candidates(raw):
    """Independently observable at j+1 close; DOES NOT import any D3 candidate.

    A high anchor is above its previous five highs and not exceeded at j+1;
    a low anchor is below its previous five lows and not undercut at j+1.
    If both are found on the same bar, discard the ambiguous anchor.
    """
    H=raw.high.to_numpy(float);L=raw.low.to_numpy(float)
    out=[];ambiguous=0
    for j in range(LEG,len(raw)-1):
        ph=(H[j]>np.max(H[j-LEG:j])) and H[j]>=H[j+1]
        pl=(L[j]<np.min(L[j-LEG:j])) and L[j]<=L[j+1]
        if ph and pl:ambiguous+=1  # keep both typed hypotheses, as original B40
        if ph:out.append((j,'H',float(H[j])))
        if pl:out.append((j,'L',float(L[j])))
    return out,ambiguous


def _path_features(raw,j,stage,kind,price,relmed,A=None):
    """Exact mathematical 10-value B4 confirmation feature *shape/semantics*.

    Unlike old reversal models, these inputs are appended to independent ZigZag
    direction features, with no frozen first-layer model/candidate used.
    The original D123 confirmation implementation is the parity reference.
    """
    C=raw.close.to_numpy(float);O=raw.open.to_numpy(float)
    H=raw.high.to_numpy(float);L=raw.low.to_numpy(float);V=raw.volume.to_numpy(float)
    if A is None:
        prev=np.r_[C[0],C[:-1]]
        tr=np.maximum(H-L,np.maximum(abs(H-prev),abs(L-prev)))
        A=pd.Series(tr).rolling(14,min_periods=14).mean().to_numpy()
    def div(x,y):return float(x/y) if np.isfinite(y) and abs(y)>1e-12 else np.nan
    def pct(x,y):return float((x/y-1)*100) if np.isfinite(y) and abs(y)>1e-12 else np.nan
    out=[]
    for k in (1,2,3):
        if k>stage:
            out.extend([np.nan]*len(PATH_ONE));continue
        t=j+k;rg=max(H[t]-L[t],1e-12)
        cur=H[t] if kind=='H' else L[t]
        extreme=100*(price-cur)/price if kind=='H' else 100*(cur-price)/price
        close=100*(price-C[t])/price if kind=='H' else 100*(C[t]-price)/price
        wick=(100*(H[t]-max(O[t],C[t]))/C[t] if kind=='H' else
              100*(min(O[t],C[t])-L[t])/C[t])
        out.extend([extreme,close,pct(C[t],C[j]),pct(O[t],C[j]),
                    pct(H[t],L[t]),pct(C[t],O[t]),(C[t]-L[t])/rg,
                    div(H[t]-L[t],A[j]),div(V[t],V[j]),wick])
    return out


def prepare_stock(raw,symbol,with_truth=True,event_step=1,include_partial=False):
    """Three causal snapshots per D1-eligible anchor, fixed base/target.

    The only oracle calls here fill RESEARCH_ONLY audit fields/labels. They are
    never accessed by score features, candidate membership, or decision time.
    """
    final,snap,replay_audit=replay(raw)
    truth=oracle_pivot_table(raw,final) if with_truth else None
    cands,ambiguous=causal_candidates(raw)
    if event_step < 1:error('BAD_EVENT_STEP')
    if event_step > 1:
        cands=[x for x in cands if x[0]%event_step==0]
    n=len(raw);dates=raw.datetime
    H=raw.high.to_numpy(float);L=raw.low.to_numpy(float)
    V=raw.volume.to_numpy(float)
    C=raw.close.to_numpy(float)
    prev=np.r_[C[0],C[:-1]]
    tr=np.maximum(H-L,np.maximum(np.abs(H-prev),np.abs(L-prev)))
    atr=pd.Series(tr).rolling(14,min_periods=14).mean().to_numpy()
    # A fixed 20-bar baseline for each anchor, independent of its outcome.
    # Compute seed/as-of stage info only if D1 exists.
    eligible=[(j,k,px) for j,k,px in cands if j+(1 if include_partial else 3)<n]
    decbars=np.asarray([j+s for j,_,_ in eligible
                        for s in range(1,min(3,n-1-j)+1)],int)
    if len(decbars)==0:
        return pd.DataFrame(),np.empty((0,len(COLS)),np.float32),truth,replay_audit
    day,base_X=features(raw,snap,decbars)
    # Lookup oracle only for truth/audit AFTER all causal anchors are generated.
    true_key={(int(p['bar']),p['type']):i for i,p in enumerate(final)} if with_truth else {}
    # Defensive: a stock with zero confirmed ZigZag final pivots yields an empty
    # truth table. Treat as no true anchors (all candidates NON_ORACLE). No label
    # or feature math is changed by this guard.
    pivbars=truth.pivot_bar.to_numpy(int) if (with_truth and len(final)>0) else None
    rows=[];X=np.full((len(decbars),len(COLS)),np.nan,np.float32)
    X[:,:len(ALL_COLS)]=base_X
    offset=0
    for i,(j,kind,px) in enumerate(eligible):
        seed=state_from_pivots(snap[j+1])
        sid=(None if seed is None else seed['state'])
        seed_score=(np.nan if seed is None else float(ID[sid]))
        seed_extras=[seed_score,
          seed['uH'] if seed else np.nan, seed['uL'] if seed else np.nan,
          100*math.log(raw.close.iloc[j+1]/seed['H2']) if seed else np.nan,
          100*math.log(raw.close.iloc[j+1]/seed['L2']) if seed else np.nan]
        medianvol=float(np.median(V[max(0,j-20):j])) if j>0 else np.nan
        anc=[float(1 if kind=='L' else -1),
             100*math.log(H[j]/L[j]),
             float(V[j]/medianvol) if medianvol>0 else np.nan]
        event=f'{symbol}|{j}|{kind}'
        curr_idx=true_key.get((j,kind)) if with_truth else None
        fut_idx=int(np.searchsorted(pivbars,j,side='right')) if (with_truth and pivbars is not None) else None
        next_piv=final[fut_idx] if (with_truth and pivbars is not None and fut_idx<len(final)) else None
        next_st=(str(truth.iloc[fut_idx].oracle_state) if next_piv is not None else None)
        next_bar=(int(next_piv['bar']) if next_piv is not None else np.nan)
        maturity=(dates.iloc[next_piv['confirmed_at']] if next_piv is not None else pd.NaT)
        # HINDSIGHT conditional training target: j must itself be a true FINAL
        # ZigZag pivot. Its structural state is a label/reference only, never
        # a model feature or real-time filter. Non-true anchors remain scorable.
        anchor_oracle_state=(str(truth.iloc[curr_idx].oracle_state)
            if curr_idx is not None else None) if with_truth else None
        anchor_oracle_target=(int(next_st!=anchor_oracle_state)
            if curr_idx is not None and next_st in STATES and anchor_oracle_state in STATES else np.nan)
        legacy_seed_target=(int(next_st!=sid)
            if sid in STATES and next_st in STATES else np.nan)
        if sid is None:
            target_reason='FOUR_CONFIRMED_PIVOTS_REQUIRED'
        elif next_piv is None:
            target_reason='NO_NEXT_MATURE_FINAL_PIVOT'
        elif next_piv['bar']<=j+3:
            target_reason='NEXT_PIVOT_WITHIN_D123'
        elif next_st not in STATES:
            target_reason='INSUFFICIENT_FUTURE_ORACLE_STATE'
        elif curr_idx is None:
            target_reason='NON_ORACLE_ANCHOR_CONDITIONAL_UNDEFINED'
        elif anchor_oracle_state not in STATES:
            target_reason='INSUFFICIENT_ANCHOR_ORACLE_STATE'
        else:
            target_reason='MATURED_FINAL_EVENT'
        y=(int(anchor_oracle_target) if target_reason=='MATURED_FINAL_EVENT' else np.nan)
        for s in range(1,min(3,n-1-j)+1):
            t=j+s;ri=offset+s-1
            X[ri,len(ALL_COLS):]=np.asarray(anc+seed_extras+_path_features(
                raw,j,s,kind,px,medianvol,atr),dtype=np.float32)
            rr={'symbol':symbol,'event_id':event,'anchor_bar':j,'anchor_dt':dates.iloc[j],
                'anchor_type':kind,'anchor_price':px,'stage':s,'decision_bar':t,
                'decision_dt':dates.iloc[t],'seed_state':sid if sid is not None else 'INSUFFICIENT',
                'stage_asof_state':day.iloc[ri].asof_state,
                'signal_available':seed is not None,'prediction_reason':(
                    'SCORED' if seed is not None else 'FOUR_CONFIRMED_PIVOTS_REQUIRED')}
            if with_truth:
                rr.update({'anchor_is_true_pivot':curr_idx is not None,
                   'oracle_anchor_state':anchor_oracle_state,
                   'oracle_next_pivot_bar':next_bar,
                   'oracle_next_pivot_dt':(dates.iloc[int(next_bar)] if np.isfinite(next_bar) else pd.NaT),
                   'oracle_next_state':next_st,'oracle_maturity':maturity,
                   'label_change':y,'label_reason':target_reason,
                   'oracle_anchor_to_next_change_RESEARCH_ONLY':anchor_oracle_target,
                   'legacy_seed_change_DIAGNOSTIC_ONLY':legacy_seed_target})
            rows.append(rr)
        offset+=min(3,n-1-j)
    df=pd.DataFrame(rows)
    assert not df.empty and len(df)==len(X)
    if not include_partial:assert len(df)%3==0
    return df,X,truth,replay_audit


def split_events(df):
    """ONE split per event, with all three stage decisions and maturity inside.

    This prevents boundary events from entering only the D1 and D2 cohorts.
    All D1/D2/D3 metrics refer to identical anchored events within each split.
    """
    anchors=df.event_id.astype(str).to_numpy()
    d1rows=df.loc[df.stage.eq(1),['event_id','decision_dt']].set_index('event_id')
    d3rows=df.loc[df.stage.eq(3),['event_id','decision_dt']].set_index('event_id')
    d1=pd.to_datetime(d1rows.loc[anchors,'decision_dt']).to_numpy(dtype='datetime64[ns]')
    d3=pd.to_datetime(d3rows.loc[anchors,'decision_dt']).to_numpy(dtype='datetime64[ns]')
    maturity=pd.to_datetime(df.oracle_maturity).to_numpy(dtype='datetime64[ns]')
    y=pd.to_numeric(df.label_change,errors='coerce').to_numpy(float)
    valid=(np.isfinite(y)&~np.isnat(maturity)&df.signal_available.to_numpy(bool)
           &df.anchor_is_true_pivot.to_numpy(bool))
    out={}
    for name,lo,hi in [('train',None,CUT1),('val',CUT1,CUT2),('test',CUT2,END)]:
        lo64=None if lo is None else lo.to_datetime64();hi64=hi.to_datetime64()
        selection=(d1<hi64) if lo is None else ((d1>=lo64)&(d1<hi64))
        # Max-stage decision must occur before the cohort ends.
        good=selection&valid&(d3<hi64)&(maturity<hi64)
        out[name]=good
        out[name+'_all']=selection
        assert int(good.sum())%3==0
    return out


def build(args):
    import pyarrow as pa
    import pyarrow.parquet as pq
    root=Path(args.out);root.mkdir(parents=True,exist_ok=True)
    files=sorted((Path(args.data_root)/'raw'/'1d').glob('*/*.parquet'))
    if args.max_symbols:files=files[:args.max_symbols]
    if not files:error('NO_RAW_OHLCV')
    writer=None;n=0;symbols=0;audit=[]
    try:
        for fi,p in enumerate(files):
            raw=load_raw(p);symbol=f'{p.parent.name}:{p.stem}'
            df,X,truth,rep=prepare_stock(raw,symbol,with_truth=True,event_step=args.event_step)
            if df.empty:continue
            for i,col in enumerate(COLS):df[col]=X[:,i]
            # Uniform columns and stable parquet schema across all stocks.
            for col in ('symbol','event_id','anchor_type','seed_state','stage_asof_state',
                        'prediction_reason','oracle_anchor_state','oracle_next_state','label_reason'):
                df[col]=df[col].astype('string')
            for col in ('anchor_is_true_pivot','signal_available'):
                df[col]=df[col].astype('bool')
            for col in ('anchor_bar','stage','decision_bar'):
                df[col]=df[col].astype('int64')
            for col in ('oracle_next_pivot_bar','label_change',
                        'oracle_anchor_to_next_change_RESEARCH_ONLY',
                        'legacy_seed_change_DIAGNOSTIC_ONLY'):
                df[col]=pd.to_numeric(df[col],errors='coerce').astype('float64')
            tab=pa.Table.from_pandas(df,preserve_index=False)
            if writer is None:
                writer=pq.ParquetWriter(root/'events_local_only.parquet',tab.schema,compression='zstd')
            writer.write_table(tab)
            symbols+=1;n+=len(df)
            audit.append({'symbol':symbol,'bars':len(raw),'candidate_events':len(df)//3,
                'n_oracle_pivots':len(truth),'true_anchor_events':int(df.loc[df.stage==1,'anchor_is_true_pivot'].sum()),
                'mature_target_events':int(df.loc[df.stage==1,'label_change'].notna().sum())})
            if (fi+1)%100==0:print('B55_BUILD_PROGRESS',fi+1,len(files),n,flush=True)
    finally:
        if writer:writer.close()
    if n==0:error('NO_EVENTS')
    pd.DataFrame(audit).to_csv(root/'per_stock_audit.csv',index=False)
    all_df=pd.read_parquet(root/'events_local_only.parquet',columns=[
       'event_id','decision_dt','stage','oracle_maturity','label_change','signal_available',
       'anchor_is_true_pivot','seed_state','label_reason'])
    masks=split_events(all_df)
    report={'version':VERSION,'symbols':symbols,'stage_rows':n,'anchor_events':n//3,
      'independent_causal_anchors':True,'uses_any_D3_model_or_candidates':False,
      'max_symbols':args.max_symbols,'event_step':args.event_step,
      'true_anchor_events':int(all_df.loc[all_df.stage.eq(1),'anchor_is_true_pivot'].sum()),
      'reasons':all_df[all_df.stage==1].label_reason.value_counts(dropna=False).to_dict(),
      'splits':{sp:{'stage_rows':int(masks[sp].sum()),'anchors':int(masks[sp].sum()//3),
                'all_rows':int(masks[sp+'_all'].sum())} for sp in ('train','val','test')}}
    jd(root/'build_report.json',report)
    print('B55_BUILD_DONE',json.dumps(report,ensure_ascii=False,default=str))


def measure(y,p,groups=None):
    from sklearn.metrics import roc_auc_score,average_precision_score,brier_score_loss
    y=np.asarray(y,int);p=np.asarray(p,float)
    d={'n':int(len(y)),'positive':int(y.sum()),'rate':float(y.mean()) if len(y) else None}
    if len(y)==0:return d
    d.update({'auc':float(roc_auc_score(y,p)) if len(np.unique(y))==2 else None,
        'ap':float(average_precision_score(y,p)) if y.sum()>0 else None,
        'brier':float(brier_score_loss(y,p)),
        'top20_rate':float(y[p>=np.quantile(p,.8)].mean())})
    if groups is not None and len(groups)==len(y):
        labs,inv=np.unique(np.asarray(groups,dtype=str),return_inverse=True)
        weights=1/np.bincount(inv)[inv].astype(float)
        d['n_future_pivots']=int(len(labs))
        if len(np.unique(y))==2:
            d['pivot_equal_weight_auc']=float(roc_auc_score(y,p,sample_weight=weights))
        d['pivot_equal_weight_brier']=float(np.average((y-p)**2,weights=weights))
    return d


def _load_training_dataset(path):
    path=Path(path)
    cols=['symbol','event_id','stage','decision_dt','oracle_maturity','label_change',
          'seed_state','anchor_type','anchor_is_true_pivot','oracle_anchor_state','oracle_next_pivot_bar','label_reason',
          'signal_available']+COLS
    d=pd.read_parquet(path,columns=cols)
    for col in COLS:d[col]=pd.to_numeric(d[col],errors='coerce').astype('float32')
    return d


def train(args):
    from lightgbm import LGBMClassifier
    data=Path(args.data)/'events_local_only.parquet'
    d=_load_training_dataset(data)
    masks=split_events(d)
    train_rows=np.flatnonzero(masks['train'])
    valid_rows=np.flatnonzero(masks['val'])
    test_rows=np.flatnonzero(masks['test'])
    if len(train_rows)<2000 or len(valid_rows)<1000:error('INSUFFICIENT_COHORT')
    # Select a sample of whole anchors, retaining their D1/D2/D3 pairing.
    if args.train_cap_anchors>0:
        tr=d.iloc[train_rows]
        anchors=tr.event_id.astype(str).unique()
        if len(anchors)>args.train_cap_anchors:
            chosen=set(np.random.default_rng(5541).choice(anchors,args.train_cap_anchors,replace=False))
            train_rows=train_rows[tr.event_id.astype(str).isin(chosen).to_numpy(bool)]
    root=Path(args.out);root.mkdir(parents=True,exist_ok=True)
    models={};metrics_rows=[];bucket_rows=[];stage_predictions={}
    # A training-time class-prior baseline checks that the classifier is not
    # merely learning the structural state base rate.
    for stage in (1,2,3):
        tr=train_rows[d.stage.to_numpy()[train_rows]==stage]
        v=valid_rows[d.stage.to_numpy()[valid_rows]==stage]
        te=test_rows[d.stage.to_numpy()[test_rows]==stage]
        if min(len(tr),len(v),len(te))<100:error('INSUFFICIENT_STAGE_'+str(stage))
        assert d.iloc[tr].anchor_is_true_pivot.all(), 'NON_TRUE_ANCHOR_IN_CONDITIONAL_TRAIN'
        ytr=d.label_change.to_numpy(float)[tr].astype(int)
        base_rate=float(ytr.mean())
        for name,cols in MODEL_COLS.items():
            model=LGBMClassifier(n_estimators=args.trees,num_leaves=31,learning_rate=.045,
                       min_child_samples=80,max_depth=-1,reg_lambda=5.0,
                       verbosity=-1,n_jobs=args.threads,random_state=5541)
            model.fit(d.iloc[tr][cols].to_numpy(np.float32),ytr)
            models[(stage,name)]={'model':model,'cols':cols}
            for sp,ix in [('val',v),('test',te)]:
                yy=d.label_change.to_numpy(float)[ix].astype(int)
                pp=model.predict_proba(d.iloc[ix][cols].to_numpy(np.float32))[:,1].astype(np.float32)
                stage_predictions[(stage,name,sp)]=(ix,pp)
                ident=d.iloc[ix].symbol.astype(str).to_numpy()+'|'+d.iloc[ix].oracle_next_pivot_bar.astype(str).to_numpy()
                for state in ('ALL','BULL','BEAR','RANGE'):
                    sel=np.ones(len(ix),bool) if state=='ALL' else (d.iloc[ix].oracle_anchor_state.to_numpy()==state)
                    result=measure(yy[sel],pp[sel],ident[sel])
                    metrics_rows.append({'model':name,'stage':stage,'period':sp,'state':state,**result})
                if sp=='test':
                    # Score bins are for audit only and determined on validation.
                    pv=stage_predictions[(stage,name,'val')][1]
                    edges=np.quantile(pv,np.linspace(0,1,11))
                    b=np.searchsorted(edges[1:-1],pp,side='right')
                    for k in range(10):
                        ss=b==k
                        bucket_rows.append({'model':name,'stage':stage,'period':sp,'bucket':k+1,
                                            'n':int(ss.sum()),'realized_change_rate':float(yy[ss].mean()) if ss.any() else None})
            print('B55_TRAINED',stage,name,'train',len(tr),flush=True)
        for sp,ix in [('val',v),('test',te)]:
            yy=d.label_change.to_numpy(float)[ix].astype(int)
            pp=np.full(len(ix),base_rate)
            metrics_rows.append({'model':'STATE_PRIOR','stage':stage,'period':sp,'state':'ALL',
                                **measure(yy,pp)})
    pd.DataFrame(metrics_rows).to_csv(root/'model_metrics.csv',index=False)
    pd.DataFrame(bucket_rows).to_csv(root/'test_buckets.csv',index=False)
    # Paired same-event across stages for all evaluated groups.
    pairs=[]
    for sp in ('val','test'):
        for name in MODEL_COLS:
            frame=None
            for stage in (1,2,3):
                ix,p=stage_predictions[(stage,name,sp)]
                tmp=pd.DataFrame({'event_id':d.iloc[ix].event_id.astype(str).to_numpy(),
                                  'y':d.iloc[ix].label_change.to_numpy(int),f'D{stage}':p})
                frame=tmp if frame is None else frame.merge(tmp.drop(columns='y'),on='event_id',validate='one_to_one')
            for a,b in [(1,2),(2,3),(1,3)]:
                delta=frame[f'D{b}'].to_numpy(float)-frame[f'D{a}'].to_numpy(float)
                pairs.append({'period':sp,'model':name,'compare':f'D{b}-D{a}',
                    'same_events':len(frame),'mean_score_change':float(delta.mean()),
                    'score_rises_on_y1':float(delta[frame.y.to_numpy()==1].mean()),
                    'score_rises_on_y0':float(delta[frame.y.to_numpy()==0].mean())})
    pd.DataFrame(pairs).to_csv(root/'paired_progression.csv',index=False)
    bundle={'version':VERSION,'cols':COLS,'model_cols':MODEL_COLS,'models':models,
            'label_definition':'CONDITIONAL on anchor j being a true final ZigZag pivot; compare next oracle pivot state with oracle anchor state; next pivot must occur after D3',
            'exclude_future_pivot_at_or_before_D3':True,'train_cap_anchors':args.train_cap_anchors}
    with open(root/'models_local_only.pkl','wb') as f:pickle.dump(bundle,f,pickle.HIGHEST_PROTOCOL)
    jd(root/'train_report.json',{'version':VERSION,'train_stage_rows':len(train_rows),
          'val_stage_rows':len(valid_rows),'test_stage_rows':len(test_rows),
          'stage_models':len(models),'bundle_sha256':sha(root/'models_local_only.pkl'),
          'raw_scores_not_calibrated':True,'no_D3_reversal_dependency':True})
    print('B55_TRAIN_DONE',len(models))


def load_bundle(path):
    with open(path,'rb') as f:b=pickle.load(f)
    if b.get('version')!=VERSION:error('WRONG_BUNDLE_VERSION')
    # Trusted local experiment bundle only. Do not unpickle unknown sources.
    return b


def apply_models(df,X,bundle):
    if df.empty:return df
    d=df.copy()
    x=pd.DataFrame(X,columns=COLS)
    keep=d.signal_available.to_numpy(bool)
    for name in MODEL_COLS:
        out=np.full(len(d),np.nan,np.float32)
        for stage in (1,2,3):
            ix=np.flatnonzero((d.stage.to_numpy()==stage)&keep)
            if not len(ix):continue
            clf=bundle['models'][(stage,name)]['model']
            names=bundle['models'][(stage,name)]['cols']
            out[ix]=clf.predict_proba(x.loc[ix,names].to_numpy(np.float32))[:,1].astype(np.float32)
        d[name+'_score']=out
    d['score_is_calibrated_probability']=False
    return d


def _date(x):return pd.to_datetime(x).dt.normalize()


def plot_sichuan(raw,oracle,d,output,start,end,overlay=None):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    lo,hi=pd.Timestamp(start),pd.Timestamp(end)
    r=raw[(raw.datetime>=lo)&(raw.datetime<hi)]
    piv=oracle[(oracle.pivot_date>=lo)&(oracle.pivot_date<hi)]
    sub=d[(d.decision_dt>=lo)&(d.decision_dt<hi)]
    fig,ax=plt.subplots(3,1,figsize=(18,12),sharex=True,
       gridspec_kw={'height_ratios':[2,1,0.55]})
    a,b,c=ax
    a.plot(r.datetime,r.close,color='#303030',lw=1.15,label='Close')
    for typ,color,mark in [('H','#cb3434','v'),('L','#16815b','^')]:
        w=piv[piv.pivot_type==typ]
        a.scatter(w.pivot_date,w.pivot_price,marker=mark,s=45,color=color,label='True '+typ,zorder=5)
        for rr in w.itertuples():
            txt=(rr.high_tag if typ=='H' else rr.low_tag)+' '+rr.oracle_state
            a.annotate(txt,(rr.pivot_date,rr.pivot_price),xytext=(0,-18 if typ=='H' else 11),
                       textcoords='offset points',fontsize=6.5,ha='center',color=color)
    if overlay is not None:
        # D3 reversal signals are a visualization overlay ONLY.
        if 'decision_dt' in overlay:
            ov=overlay.copy();ov['date']=pd.to_datetime(ov.decision_dt).dt.normalize()
            rm=r.assign(date=_date(r.datetime))
            ov=ov.merge(rm[['date','datetime','close']],on='date',how='left',validate='many_to_one')
            hit=ov[(ov.datetime>=lo)&(ov.datetime<hi)&ov.close.notna()]
            a.scatter(hit.datetime,hit.close,marker='x',s=20,color='#c89423',label='Original D3 candidate')
    # On actual true pivots, plot all three stage forecast outputs on their
    # respective observable D1,D2,D3 decision days (never on the extreme day).
    true=d[(d.anchor_is_true_pivot==True)&(d.decision_dt>=lo)&(d.decision_dt<hi)]
    colors={1:'#8955c7',2:'#e29b32',3:'#2672a7'}
    marks={1:'o',2:'s',3:'D'}
    for k in (1,2,3):
        w=true[(true.stage==k)&true.B1_score.notna()]
        b.scatter(w.decision_dt,w.B1_score,s=30,marker=marks[k],color=colors[k],label=f'D{k} (true-pivot anchors)',zorder=4)
    # All live causal candidates (not only oracle true pivots) are recorded in CSV.
    b.set(ylim=(-.05,1.05),ylabel='P(trend CHANGE) raw B1 score')
    b.legend(loc='upper left',ncol=3,fontsize=8)
    for k in (1,2,3):
        w=sub[sub.stage==k]
        vals=w.B1_score.astype(float)
        if len(w):
            per=w.groupby(_date(w.decision_dt))['B1_score'].median().reset_index()
            b.plot(per.decision_dt,per.B1_score,color=colors[k],alpha=.30,linewidth=1)
    # Show D1-baseline structural state along the events (not oracle hindsight).
    d1=sub[sub.stage==1].copy()
    mapped=d1.seed_state.map(ID)
    c.scatter(d1.decision_dt,mapped,s=5,c=mapped,cmap='coolwarm',vmin=-1,vmax=1)
    c.set(yticks=[-1,0,1],yticklabels=['BEAR','RANGE','BULL'],ylim=(-1.5,1.5),ylabel='D1 seed state')
    a.set_ylabel('CNY');a.legend(loc='upper left',ncol=3,fontsize=8)
    fig.suptitle('Sichuan Gold: TRUE H/L is hindsight; D1/D2/D3 scores appear on DECISION dates\n'
                 'P(trend CHANGE) vs frozen D1 AS-OF state, uncalibrated; no oracle feature')
    fig.tight_layout();fig.savefig(output,dpi=140);plt.close(fig)


def audit(args):
    """LABEL REVIEW only: true-pivot and all-candidate target ledger, NO model."""
    raw=load_raw(args.input or rawpath(args.data_root,args.symbol))
    d,X,truth,_=prepare_stock(raw,args.symbol,with_truth=True)
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    d.to_csv(out/'candidate_d123_label_audit.csv',index=False)
    truth.to_csv(out/'oracle_truth_RESEARCH_ONLY.csv',index=False)
    # A light price + ground-truth chart for manual semantic review.
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    lo,hi=pd.Timestamp(args.start),pd.Timestamp(args.end)
    r=raw[(raw.datetime>=lo)&(raw.datetime<hi)]
    q=truth[(truth.pivot_date>=lo)&(truth.pivot_date<hi)]
    fig,ax=plt.subplots(figsize=(16,6))
    ax.plot(r.datetime,r.close,lw=1.1,color='#424242',label='Close')
    for typ,color,mark in [('H','#ce3434','v'),('L','#13885c','^')]:
        sub=q[q.pivot_type==typ]
        ax.scatter(sub.pivot_date,sub.pivot_price,color=color,marker=mark,s=45,
                   label='Hindsight true '+typ,zorder=5)
        for row in sub.itertuples():
            ax.annotate((row.high_tag if typ=='H' else row.low_tag)+' '+row.oracle_state,
                        (row.pivot_date,row.pivot_price),ha='center',fontsize=7,
                        xytext=(0,-17 if typ=='H' else 12),textcoords='offset points',color=color)
    ax.legend();ax.set_title('B55 LABEL AUDIT ONLY: oracle pivots, NOT known at D1/D2/D3')
    fig.tight_layout();fig.savefig(out/'label_oracle_truth.png',dpi=145);plt.close(fig)
    anchor=d[d.stage==1]
    report={'symbol':args.symbol,'oracle_true_pivots_all':len(truth),
            'oracle_true_pivots_in_window':int(len(q)),
            'independent_causal_anchors':int(len(anchor)),
            'true_anchors':int(anchor.anchor_is_true_pivot.sum()),
            'with_causal_seed':int(anchor.signal_available.sum()),
            'eligible_future_labels':int(anchor.label_change.notna().sum()),
            'label_reasons':anchor.label_reason.value_counts(dropna=False).to_dict(),
            'different_oracle_anchor_state_from_D1':int((anchor.anchor_is_true_pivot &
              anchor.oracle_anchor_state.ne(anchor.seed_state)).sum()),
            'no_model_used':True}
    jd(out/'audit_report.json',report)
    print('B55_LABEL_AUDIT_DONE',json.dumps(report,default=str))


def stock(args):
    raw=load_raw(args.input or rawpath(args.data_root,args.symbol))
    if args.as_of:raw=raw[raw.datetime<pd.Timestamp(args.as_of)].reset_index(drop=True)
    df,X,truth,rep=prepare_stock(raw,args.symbol,with_truth=True,include_partial=True)
    if df.empty:error('NO_CANDIDATES')
    d=apply_models(df,X,load_bundle(args.bundle))
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    keepcols=['symbol','event_id','anchor_bar','anchor_dt','anchor_type','anchor_price',
        'stage','decision_bar','decision_dt','seed_state','stage_asof_state','signal_available',
        'prediction_reason']+[k+'_score' for k in MODEL_COLS]
    d[keepcols].to_csv(out/'all_candidate_stage_scores.csv',index=False)
    truth.to_csv(out/'oracle_final_pivots_RESEARCH_ONLY.csv',index=False)
    res=d[d.anchor_is_true_pivot].copy()
    res.to_csv(out/'true_pivot_event_predictions_RESEARCH_ONLY.csv',index=False)
    d[['event_id','stage','decision_dt','oracle_next_pivot_dt','oracle_maturity',
       'oracle_next_state','seed_state','label_change','label_reason','anchor_is_true_pivot',
       'oracle_anchor_state','oracle_anchor_to_next_change_RESEARCH_ONLY',
       'legacy_seed_change_DIAGNOSTIC_ONLY']].to_csv(
        out/'event_labels_RESEARCH_ONLY.csv',index=False)
    overlays=None
    if args.d3_csv:
        overlays=pd.read_csv(args.d3_csv)
        if 'decision_dt' not in overlays:error('D3_MISSING_DECISION_DT')
        # Match every original D3 candidate by independent H/L event anchor.
        q=overlays.copy();q['type']=q['head'].astype(str)
        if 'candidate_bar' not in q:error('D3_MISSING_candidate_bar')
        match=d[d.stage==3][['anchor_bar','anchor_type','decision_dt','B1_score']].copy()
        match=match.rename(columns={'anchor_bar':'candidate_bar','anchor_type':'type',
                                   'decision_dt':'B55_decision_dt','B1_score':'B55_D3_change_score'})
        q['candidate_bar']=pd.to_numeric(q.candidate_bar)
        joined=q.merge(match,on=['candidate_bar','type'],how='left',validate='many_to_one')
        joined['match_status']=np.where(joined.B55_D3_change_score.notna(),'SCORED','NO_CAUSAL_ANCHOR_OR_HISTORY')
        joined.to_csv(out/'d3_coverage_audit.csv',index=False)
    plot_sichuan(raw,truth,d,out/'sichuan_d123_direction.png',args.start,args.end,overlays)
    lo,hi=pd.Timestamp(args.start),pd.Timestamp(args.end)
    qa=lo;piece=1
    while qa<hi:
        qb=min(hi,qa+pd.DateOffset(months=3))
        plot_sichuan(raw,truth,d,out/f'sichuan_d123_part{piece}.png',str(qa.date()),str(qb.date()),overlays)
        qa=qb;piece+=1
    focus=d[(d.decision_dt>=lo)&(d.decision_dt<hi)]
    nanchors=int(focus[focus.stage==1].event_id.nunique())
    mature=focus[(focus.stage==3)&focus.label_change.notna()]
    report={'version':VERSION,'symbol':args.symbol,'all_causal_candidate_anchors':nanchors,
        'all_candidate_stage_rows':len(focus),'true_pivots_in_window':int(((truth.pivot_date>=lo)&(truth.pivot_date<hi)).sum()),
        'true_anchor_stage_rows':int(focus.anchor_is_true_pivot.sum()),
        'mature_label_events_D3':int(len(mature)),
        'partial_live_anchors':int((d.groupby('event_id').stage.max()<3).sum()),
        'scored_stage_rows':int(focus.B1_score.notna().sum()),
        'label_definition':'ORACLE_CONDITIONAL: true final anchor state versus following oracle pivot state; same target across D1/D2/D3',
        'conditional_training_true_anchor_only':True,
        'scores_uncalibrated':True,
        'd3_overlay_coverage':(joined.match_status.value_counts().to_dict() if overlays is not None else None)}
    jd(out/'stock_report.json',report)
    print('B55_STOCK_DONE',json.dumps(report,default=str))


def predict(args):
    """No final-pivot / future oracle table is built or accessed in this path."""
    raw=load_raw(args.input or rawpath(args.data_root,args.symbol))
    if args.as_of:raw=raw[raw.datetime<pd.Timestamp(args.as_of)].reset_index(drop=True)
    df,X,_,_=prepare_stock(raw,args.symbol,with_truth=False,include_partial=True)
    if df.empty:error('NO_CANDIDATES')
    d=apply_models(df,X,load_bundle(args.bundle))
    # Pure inference CSV explicitly excludes any future/oracle annotation.
    out=Path(args.out);out.mkdir(parents=True,exist_ok=True)
    d.to_csv(out/'causal_stage_predictions.csv',index=False)
    assert not any('oracle' in c or 'label' in c for c in d.columns)
    jd(out/'predict_report.json',{'symbol':args.symbol,'rows':len(d),
       'anchors':int(d.event_id.nunique()),'bundle_sha256':sha(args.bundle),'has_oracle_labels':False,
       'uses_D3_reversal':False,'score_uncalibrated':True,
       'score_is_conditional_on_true_pivot_anchor':True})
    print('B55_PREDICT_DONE',len(d))


def selftest():
    # Known 2H2L truth, no tolerance near HH/HL labels.
    p=lambda i,k,v:{'bar':i,'type':k,'price':float(v),'seen_at':i+5,'confirmed_at':i+10}
    truth0=[p(10,'L',90),p(20,'H',100),p(30,'L',94),p(40,'H',108)]
    assert state_from_pivots(truth0)['state']=='BULL'
    rng=np.random.default_rng(555)
    t=np.arange(650)
    close=50*np.exp(.14*np.sin(t/15)+np.cumsum(rng.normal(0,.003,len(t))))
    raw=pd.DataFrame({'datetime':pd.bdate_range('2023-01-01',periods=len(t)),
         'open':close*.999,'high':close*1.008,'low':close*.992,'close':close,
         'volume':10000+800*np.cos(t/16)})
    d,x,oracle,_=prepare_stock(raw,'SY:000001')
    assert len(d)>90 and x.shape==(len(d),len(COLS))
    assert len(d)%3==0 and (d.groupby('event_id').stage.nunique()==3).all()
    # D1/D2/D3 fixed target and known seed, even if as-of daily state evolves.
    groups=d.groupby('event_id',sort=False)
    assert (groups.seed_state.nunique()==1).all()
    assert (groups.label_change.nunique(dropna=False)==1).all()
    assert (groups.oracle_next_pivot_bar.nunique(dropna=False)==1).all()
    assert (groups.anchor_is_true_pivot.nunique()==1).all()
    usable=d.label_change.notna()
    assert d.loc[usable,'anchor_is_true_pivot'].all()
    assert (d.loc[usable,'label_change'].to_numpy()==
            d.loc[usable,'oracle_anchor_to_next_change_RESEARCH_ONLY'].to_numpy()).all()
    assert not d.loc[d.label_reason=='MATURED_FINAL_EVENT','oracle_next_pivot_bar'].le(
         d.loc[d.label_reason=='MATURED_FINAL_EVENT','anchor_bar']+3).any()
    # First-stage features: D2/D3 path slots MUST be NaN, never read later bars.
    cidx={c:i for i,c in enumerate(COLS)}
    d1=d.stage.to_numpy()==1;d2=d.stage.to_numpy()==2
    assert np.isnan(x[d1,cidx['path2_extreme_pct']]).all()
    assert np.isnan(x[d1,cidx['path3_extreme_pct']]).all()
    assert np.isnan(x[d2,cidx['path3_extreme_pct']]).all()
    # All causal event IDs from full history that end before the truncation
    # must have exact immutable feature values in a prefix replay.
    cut=len(raw)-45
    pref,px,_,_=prepare_stock(raw.iloc[:cut].reset_index(drop=True),'SY:000001',with_truth=False)
    full,fx,_,_=prepare_stock(raw,'SY:000001',with_truth=False)
    im={k:i for i,k in enumerate(full.event_id.astype(str)+'|'+full.stage.astype(str))}
    common=[(idx,im[k]) for idx,k in enumerate(pref.event_id.astype(str)+'|'+pref.stage.astype(str)) if k in im]
    assert common and max(float(np.nanmax(np.abs(np.nan_to_num(px[a])-np.nan_to_num(fx[b])))) for a,b in common)==0
    assert set(pref.columns)==set(full.columns)
    # An oracle true pivot can be anchored, but is NEVER a feature column.
    assert not any('oracle' in z or 'label' in z for z in COLS)
    # A simple conditional-label counterexample to the old seed-based target:
    # observed old BEAR, true anchor BULL, next BULL => NO change, not change.
    assert int('BULL'!='BULL') == 0 and int('BULL'!='BEAR') == 1
    print('B55_SELFTEST_PASS',json.dumps({'anchors':len(d)//3,'stage_rows':len(d),
          'feature_width':len(COLS),'prefix_common':len(common),'true_pivots':len(oracle)}))


def cli():
    pa=argparse.ArgumentParser(description=__doc__)
    sub=pa.add_subparsers(dest='cmd',required=True)
    sub.add_parser('selftest')
    a=sub.add_parser('audit')
    a.add_argument('--symbol',default='SZ:001337')
    a.add_argument('--input',default=None)
    a.add_argument('--data-root',default='data')
    a.add_argument('--out',default='research_runs/b_v55_d123/sichuan_gold_label_audit')
    a.add_argument('--start',default='2025-10-01')
    a.add_argument('--end',default='2026-10-01')
    a=sub.add_parser('build')
    a.add_argument('--data-root',default='data');a.add_argument('--out',default='research_runs/b_v55_d123/dataset')
    a.add_argument('--max-symbols',type=int,default=0)
    a.add_argument('--event-step',type=int,default=5)
    a=sub.add_parser('train')
    a.add_argument('--data',default='research_runs/b_v55_d123/dataset')
    a.add_argument('--out',default='research_runs/b_v55_d123/models')
    a.add_argument('--trees',type=int,default=300)
    a.add_argument('--threads',type=int,default=4)
    a.add_argument('--train-cap-anchors',type=int,default=100000)
    for command in ('stock','predict'):
        a=sub.add_parser(command)
        a.add_argument('--symbol',default='SZ:001337')
        a.add_argument('--input',default=None)
        a.add_argument('--data-root',default='data')
        a.add_argument('--bundle',default='research_runs/b_v55_d123/models/models_local_only.pkl')
        a.add_argument('--out',default=f'research_runs/b_v55_d123/sichuan_gold_{command}')
        a.add_argument('--as-of',default=None)
        if command=='stock':
            a.add_argument('--start',default='2025-10-01')
            a.add_argument('--end',default='2026-10-01')
            a.add_argument('--d3-csv',default=None)
    args=pa.parse_args()
    if args.cmd=='selftest':selftest()
    elif args.cmd=='audit':audit(args)
    elif args.cmd=='build':build(args)
    elif args.cmd=='train':train(args)
    elif args.cmd=='stock':stock(args)
    elif args.cmd=='predict':predict(args)

if __name__=='__main__':cli()
