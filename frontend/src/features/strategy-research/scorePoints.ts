import type {Candidate} from '@/api/strategyCandidates'
export const SCORE_FAMILIES = [
  {id:'D0_H',label:'D0-H 极值高点',color:'#ffb454'}, {id:'D0_L',label:'D0-L 极值低点',color:'#55cce8'},
  {id:'L1_H',label:'L1-H 高点确认',color:'#f071a9'}, {id:'L1_L',label:'L1-L 低点确认',color:'#91d17b'},
  {id:'L2_H',label:'L2-H 向下突破',color:'#b39bff'}, {id:'L2_L',label:'L2-L 向上突破',color:'#5b9fff'},
] as const
export interface ScorePoint {id:string;family:string;candidateId:string;observationId:string;stage:string;date:string;anchorDate:string;score:number;threshold:number;color:string}
export const scoreValue=(p:Pick<ScorePoint,'score'|'threshold'>,raw:boolean)=>raw?p.score:p.score-p.threshold
export function scorePoints(candidates:Candidate[]):ScorePoint[]{
  return candidates.flatMap(c=>c.observations.flatMap(o=>(['D0','L1','L2'] as const).flatMap(layer=>{
    const [score,threshold]=layer==='D0'?[o.d0_score,o.d0_threshold]:layer==='L1'?[o.l1_score,o.l1_threshold]:[o.l2_score,o.l2_threshold]
    if(score==null||threshold==null||!Number.isFinite(score)||!Number.isFinite(threshold))return []
    const family=`${layer}_${c.kind}`,color=SCORE_FAMILIES.find(f=>f.id===family)!.color
    return [{id:`${o.observation_id}:${layer}`,family,candidateId:c.candidate_id,observationId:o.observation_id,stage:o.stage,date:o.decision_date,anchorDate:c.anchor_date,score,threshold,color}]
  })))
}
