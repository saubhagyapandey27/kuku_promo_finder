"""Preference ranking after semantic verification. Ranking has no additional veto authority."""
import json
import random
from pathlib import Path
from . import prompts,rank,boundary
from .llm.base import BudgetExceeded
from .schemas import ListwiseRanking
from .util import write_json,read_json,sha


def proof_fingerprint(p,cfg):
    return sha('preference-only-1',p.editorial.get('fingerprint'),prompts.load_prompt('final_rank'),
        ListwiseRanking.model_json_schema(),cfg.profile.jury,
        [dict(id=s.id,effort=s.effort,family=s.family) for s in cfg.role_prefs('jury')],n=32)


def valid_proof(p,renderer,cfg):
    try:
        proof=p.editorial['jury_proof']
        if proof['fingerprint']!=proof_fingerprint(p,cfg):
            return False
        expected={(r,s) for r in cfg.profile.jury.get('rounds',['R1']) for s in proof['seats']}
        rows=p.editorial['jury_reviews']
        if len(proof['seats'])!=int(cfg.profile.jury.get('seats',2)):
            return False
        if not set(proof['seats'])<={s.label for s in cfg.role_prefs('jury')}:
            return False
        return (len(rows)==len(expected) and {(r['round'],r['model']) for r in rows}==expected
                and all(isinstance(r['position'],int) and r['position']>=1 for r in rows))
    except (KeyError,ValueError,TypeError):
        return False


def rank_eligible(pipe,ordered,renderer):
    digest=sha([(p.id,proof_fingerprint(p,pipe.cfg)) for p in ordered],n=32)
    saved=read_json(pipe.p('quality_jury.json'))
    lookup={p.id:p for p in ordered}
    if saved and saved.get('fingerprint')==digest:
        for pid,rows in saved.get('reviews',{}).items():
            lookup[pid].editorial.update(jury_reviews=rows,jury_proof=saved['proofs'][pid])
        if saved.get('unassessed_ids'):
            pipe.quality_incomplete=True
        return [lookup[pid] for pid in saved['ranking'] if valid_proof(lookup[pid],renderer,pipe.cfg)]
    ids=list(lookup)
    sizes=rank.group_sizes(len(ids)) if len(ids)>1 else [1]
    groups=rank.snake_groups(ids,len(sizes)) if len(ids)>1 else [ids]
    calls,errors=[],[]
    seats=pipe.router.jury_seats()
    exhausted=False
    for round_no in pipe.cfg.profile.jury.get('rounds',['R1']):
        if round_no!='R1' and ids:
            groups=rank.rotated_groups(ids,groups,len(sizes),sizes=sizes)
        for seat in seats:
            for i,group in enumerate(groups):
                if not group or exhausted:
                    continue
                shown=list(group)
                random.Random(f'{round_no}:{seat.label}:{i}').shuffle(shown)
                labels={chr(65+j):pid for j,pid in enumerate(shown)}
                items='\n\n'.join('PROMO '+label+':\n'+renderer.render(boundary.as_segments(lookup[pid]))
                                   for label,pid in labels.items())
                system,user=prompts.build('final_rank',items=items)
                try:
                    result=pipe.router.complete_json('jury',system,user,ListwiseRanking,spec=seat,max_tokens=2048,
                        stage='final_rank',item_id=f'{round_no}:{seat.label}:{i}',tries=1,attempt_limit=1,schema_retries=0)
                    order=rank._valid_ranking(result.obj,labels)
                    if order is None:
                        raise ValueError('preference ranking missing or duplicate labels')
                    calls.append({'seat':seat.label,'round':round_no,'shown':shown,'ranking':order,
                                  'notes':[n.model_dump() for n in result.obj.notes]})
                except BudgetExceeded:
                    exhausted=True
                    errors.append('relative preference budget exhausted')
                except Exception as exc:
                    errors.append(type(exc).__name__+': '+str(exc)[:150])
    required={(r,s.label) for r in pipe.cfg.profile.jury.get('rounds',['R1']) for s in seats}
    reviews={pid:[{'round':c['round'],'model':c['seat'],'position':c['ranking'].index(pid)+1}
                  for c in calls if pid in c['ranking']] for pid in ids}
    unassessed=[pid for pid in ids if {(r['round'],r['model']) for r in reviews[pid]}!=required]
    if len(seats)!=int(pipe.cfg.profile.jury.get('seats',2)):
        unassessed=ids
    if unassessed:
        pipe.quality_incomplete=True
    survivors=[pid for pid in ids if pid not in unassessed]
    for pid in ids:
        lookup[pid].editorial.update(jury_reviews=reviews[pid],jury_proof={
            'fingerprint':proof_fingerprint(lookup[pid],pipe.cfg),'seats':[s.label for s in seats]})
    graph={pid:set() for pid in survivors}
    for call in calls:
        members=set(call['ranking'])&set(survivors)
        for pid in members:
            graph[pid].update(members-{pid})
    unseen=set(survivors)
    components=[]
    while unseen:
        stack=[unseen.pop()]
        component=set(stack)
        while stack:
            for pid in graph[stack.pop()]&unseen:
                unseen.remove(pid)
                component.add(pid)
                stack.append(pid)
        components.append(sorted(component))
    if len(components)==1 and len(survivors)>1:
        preferences=[[pid for pid in c['ranking'] if pid in survivors] for c in calls]
        strengths=rank.fit_pl(survivors,[p for p in preferences if len(p)>1])
        survivors.sort(key=lambda pid:-strengths[pid])
        for pid in survivors:
            lookup[pid].scores['pl_score']=round(strengths[pid],5)
    record={'fingerprint':digest,'calls':calls,'comparisons':len(calls),'errors':errors,
        'withheld_ids':[],'objections':{},'unassessed_ids':unassessed,'reviews':reviews,
        'proofs':{pid:lookup[pid].editorial['jury_proof'] for pid in ids},
        'comparison_components':components,'ranking':survivors,'eligible_entrants':len(ids)}
    write_json(pipe.p('quality_jury_history/'+digest+'.json'),record)
    write_json(pipe.p('quality_jury.json'),record)
    Path(pipe.p('08_listwise.jsonl')).write_text(''.join(json.dumps(c)+'\n' for c in calls),encoding='utf-8')
    return [lookup[pid] for pid in survivors]
