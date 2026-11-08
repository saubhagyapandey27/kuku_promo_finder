"""Explicit editorial rubric and source-time overlap suppression; no preference service."""
from .util import sha

VERSION='rubric-1'
WEIGHTS={'H':.35,'T':.30,'C':.20,'P':.15}

def intervals(p):
    out={}
    for s in p.segments:
        out.setdefault(s['ep'],[]).append((s['start_s'],s['end_s']))
    return out

def temporal_iou(a,b):
    aa,bb=intervals(a),intervals(b)
    intersection=sum(max(0,min(z,v)-max(x,u)) for ep,rows in aa.items() for x,z in rows for u,v in bb.get(ep,[]))
    union=sum(z-x for rows in aa.values() for x,z in rows)+sum(z-x for rows in bb.values() for x,z in rows)-intersection
    return intersection/union if union else 0

def score(p,weights=None):
    w=weights or WEIGHTS
    if set(w)!=set(WEIGHTS) or abs(sum(w.values())-1)>1e-6 or any(v<0 for v in w.values()):
        raise ValueError('rubric weights must be nonnegative and sum to one')
    a=p.editorial['assessment']; h=p.editorial['hook_assessment']
    values={'H':h['stop_power'],'T':a['cliff'],'C':a['clarity'],'P':a['pacing']}
    if any(not isinstance(v,int) or not 1<=v<=5 for v in values.values()):
        raise ValueError('all rubric inputs require integer scores 1–5')
    p.scores.update(composite=round(20*sum(w[k]*v for k,v in values.items()),2),hook=values['H'],
        cliff_pull=values['T'],comprehension=values['C'],pacing=values['P'])
    p.editorial['ranking']={'method':VERSION,'inputs':values,'weights':w,'score':p.scores['composite'],
        'fingerprint':sha(p.editorial['fingerprint'],values,w,VERSION,n=32)}
    p.scores['first_post']=values['H']>=4 and values['T']>=4 and min(values['C'],values['P'])>=3 and not (h['critical_uncertainty'] or a['critical_uncertainty'])

def rank_promos(promos,weights=None,threshold=.60):
    for p in promos:
        score(p,weights)
    ordered=sorted(promos,key=lambda p:(-p.scores['composite'],-p.scores['hook'],-p.scores['cliff_pull'],p.id))
    selected=[]
    for p in ordered:
        other=next((q for q in selected if temporal_iou(p,q)>=threshold),None)
        if other:
            other.editorial.setdefault('alternate_ids',[]).append(p.id)
            other.editorial.setdefault('alternates',[]).append({'id':p.id,'segments':p.segments,'score':p.scores['composite'],'iou':round(temporal_iou(p,other),4)})
        else:
            selected.append(p)
    return selected

def posting_plan(promos):
    strong=[p.id for p in promos if p.scores.get('first_post')]
    return {'posting_order':[p.id for p in promos], 'first_post_shortlist':strong[:5],
        'top_five':[p.id for p in promos[:5]],'method':VERSION,'note':'Editorial weights; no audience conversion measurement.'}
