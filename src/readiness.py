"""Blind consequence -> source closure verification. No source-authored target or referee."""
import json
from . import boundary, prompts, rank
from .editorial import critic_coverage_complete
from .llm.base import BudgetExceeded, TruncatedError
from .schemas import FinalViewer, ReadinessBatch, ReadinessItem,ClosureIssue,ReadinessEvidence
from .util import read_json, write_json, sha

VERSION='question-closure-witness-6'


def identity(spec):
    return {'model':spec.label,'model_id':spec.id,'family':spec.family}


def source_material(p, renderer):
    """Bounded factual source, constructed AFTER the blind response; no intended consequence."""
    show=renderer.show
    selected=boundary.as_segments(p)
    gaps=[]
    for a,b in zip(selected,selected[1:]):
        if a.ep==b.ep:
            gaps.extend(x.model_dump(include={'id','ep','kind','text','speaker','t0','t1'}) for x in show.episode(a.ep).blocks
                        if x.t0>=a.end_s and x.t1<=b.start_s)
    last=selected[-1]
    ep=show.episode(last.ep)
    continuation=[b.model_dump(include={'id','ep','kind','text','speaker','t0','t1'}) for b in ep.blocks if b.t1>last.end_s and b.t0<last.end_s+45]
    if ep.duration-last.end_s<15 and show.episode(last.ep+1):
        continuation.extend(b.model_dump(include={'id','ep','kind','text','speaker','t0','t1'}) for b in show.episode(last.ep+1).blocks if b.t0<20)
    return {'cut_end':{'ep':last.ep,'time_s':last.end_s},
            'postcut_source_continuation':continuation[:60],'omitted_source_gaps':gaps[:60]}


def proof_fingerprint(p):
    return sha(VERSION,p.editorial.get('fingerprint'),p.editorial.get('assessment'),
               p.editorial.get('closure_context_hash'),prompts.load_prompt('readiness'),
               ReadinessBatch.model_json_schema(),n=32)


def canonical_quotes(evidence, content, kinds):
    changes=[]
    for e in evidence:
        quote=e.quote.strip()
        prefix=kinds.get(e.block_id,'')+': '
        if prefix!=': ' and quote.startswith(prefix):
            body=quote[len(prefix):].strip()
            if body and body in content.get(e.block_id,''):
                changes.append({'block_id':e.block_id,'original':e.quote,'canonical':body})
                e.quote=body
        if not e.quote.strip() or e.quote.strip() not in content.get(e.block_id,''):
            raise ValueError('quote must match content, not metadata or an omitted event')
    return changes


def validate_cold(item, p, view):
    """Clip-only evidence membership. No source claim or title proof is consulted."""
    changes=canonical_quotes(item.evidence,view.content,{b['id']:b['kind'] for b in view.source})
    required=item.setup_quote_ids+item.terminal_quote_ids
    if any(b not in view.content for b in required) or p.cliff_block not in item.terminal_quote_ids:
        raise ValueError('cold tuple must quote selected setup and actual terminal anchor')
    quoted={e.block_id for e in item.evidence}
    for bid in dict.fromkeys(required):
        if bid not in quoted:
            exact=view.content[bid]
            item.evidence.append(ReadinessEvidence(block_id=bid,quote=exact))
            changes.append({'block_id':bid,'original':None,'canonical':exact,
                            'rule':'materialized exact selected content for model-cited ID'})
    return changes


def validate_item(item,p,view,context=None):
    context=context or {}
    rows=context.get('postcut_source_continuation',[])+context.get('omitted_source_gaps',[])
    content=view.content|{k:v['text'] for k,v in view.uncertainty.items()}|{b['id']:b['text'] for b in rows}
    # Source critic may quote either the anonymous display or the exact fully
    # selected source event. Never admit a full straddling event as observed.
    selected_content=dict(view.content)
    for b in view.source:
        if b['id'] in view.full_ids:
            selected_content[b['id']]=view.content.get(b['id'],'')+'\n'+b['text']
            content[b['id']]=selected_content[b['id']]
    kinds={b['id']:b['kind'] for b in view.source+rows}
    witnesses=[e for k in ('source_clarification','in_clip_payoff','reopening_trigger','postcut_response') for e in getattr(item,k)]
    evidence=list(item.evidence)+witnesses+[e for issue in item.hard_veto_issues for e in issue.evidence]
    changes=canonical_quotes(evidence,content,kinds)
    if not any(e.block_id in view.content for e in item.evidence):
        raise ValueError('closure review needs selected-cut evidence, not source continuation alone')
    if any(e.block_id not in view.content for e in item.in_clip_payoff+item.reopening_trigger):
        raise ValueError('payoff/reopening witnesses must be asserted selected content')
    canonical_quotes(item.in_clip_payoff+item.reopening_trigger,selected_content,kinds)
    return changes


def enforce_witness_chronology(item,view,context):
    """A later rewarding payoff closes an earlier open threat. Only a later selected trigger reopens it."""
    rows={b['id']:b for b in view.source+context.get('postcut_source_continuation',[])+context.get('omitted_source_gaps',[])}
    changes=[]
    def append(code,evidence,reason):
        if not any(i.code.value==code for i in item.issues):
            item.issues.append(ClosureIssue(code=code,evidence=[evidence],reason=reason))
        if item.payoff_withheld_at_cut:
            changes.append({'rule':code,'original_payoff_withheld_at_cut':True,'derived':False})
        item.payoff_withheld_at_cut=False
    if item.in_clip_payoff:
        payoff=rows[item.in_clip_payoff[0].block_id]
        reopen=rows.get(item.reopening_trigger[0].block_id) if item.reopening_trigger else None
        later=bool(reopen and (reopen['ep'],reopen['t0'])>=(payoff['ep'],payoff['t1']-.02))
        if not later:
            append('EMOTIONAL_STAKES_SETTLED',item.in_clip_payoff[0],
                   'The chosen question is answered without a later selected new choice, obstacle or mystery trigger.')
    if item.postcut_response and item.postcut_response[0].quote.strip() in view.content.get(item.postcut_response[0].block_id,''):
        append('PAYOFF_ALREADY_SHOWN',item.postcut_response[0],
               'The claimed withheld-response witness is already in the selected clip.')
    return changes


def partial_spoiler_issues(view):
    """Potential result inside an unanchored straddling clause cannot be certified absent."""
    return [ClosureIssue(code='UNANCHORED_PARTIAL_SHOT_SPOILER',
        evidence=[ReadinessEvidence(block_id=bid,quote=c['quote'])],
        reason='Unanchored result clause straddles the cut; trim before this source block.')
        for bid,row in view.uncertainty.items() for c in row.get('unanchored_clauses',[])
        if c.get('possible_payoff')]


def enforce_partial_safety(item,view):
    # The source critic sees every uncertain clause and must connect a veto to THIS question.
    # The lexical detector supplies diagnostics, never a universal result-word veto.
    return item


def valid_proof(p,renderer=None,cfg=None):
    proof=p.editorial.get('readiness',{})
    try:
        if renderer and p.editorial.get('closure_context_hash')!=sha(source_material(p,renderer),n=32):
            return False
        if proof.get('fingerprint')!=proof_fingerprint(p):
            return False
        reviews=proof['reviews']
        expected=proof['models']
        if not critic_coverage_complete(expected,reviews):
            return False
        if cfg:
            seats=cfg.critic_seats(cfg.generator_family())
            if expected!=[identity(s) for s in seats]:
                return False
        if {r['model'] for r in reviews}!={r['model'] for r in expected}:
            return False
        for row in reviews:
            item=ReadinessItem.model_validate(row)
            if renderer:
                enforce_partial_safety(item,renderer.evidence(boundary.as_segments(p)))
            if item.verdict!='ready' or not row.get('request_hash'):
                return False
            if {k:row[k] for k in ('model','model_id','family')} not in expected:
                return False
            if renderer:
                view=renderer.evidence(boundary.as_segments(p))
                context=source_material(p,renderer)
                validate_item(item,p,view,context)
                enforce_witness_chronology(item,view,context)
                if item.verdict!='ready':
                    return False
        return True
    except (ValueError,KeyError,TypeError):
        return False


def answered_control(pipe,p,renderer):
    """A continuation comparison, never an assumed answer or required setup."""
    if not hasattr(pipe,'cutter'):
        return None
    last=p.segments[-1]
    blocks=[b for b in renderer.show.episode(last['ep']).blocks if b.kind=='LINE' and b.t0>=last['end_s']]
    for b in blocks[1:4]:
        q=p.model_copy(deep=True)
        q.pick[-1]=(q.pick[-1][0],b.id)
        q.cliff_block,q.cliff_quote=b.id,b.text
        result=pipe.cutter.cut(boundary.spec_for(q))
        if result.ok:
            return {'comparison_end':b.id,'known_answer':False,'clip':renderer.render(result.segments)}
    return None


def critic_packet(pipe,p,renderer):
    view=renderer.evidence(boundary.as_segments(p))
    a=FinalViewer.model_validate(p.editorial['assessment'])
    return {'terminal_anchor':p.cliff_block,'selected_cut':view.text,
            'selected_source_events':view.source,
            'cold_viewer_tuple':{k:v for k,v in a.model_dump().items() if k not in ('cliff','clarity','pacing','why_it_works','title','biggest_weakness')},
            **source_material(p,renderer),'partial_shot_uncertainty':view.uncertainty}


def validate_batch(obj,labels,renderer):
    for item in obj.items:
        item.id=rank._norm_label(item.id)
    if sorted(i.id for i in obj.items)!=sorted(labels):
        raise ValueError('missing or duplicate closure labels')
    for item in obj.items:
        p=labels[item.id]
        validate_item(item,p,renderer.evidence(boundary.as_segments(p)),source_material(p,renderer))


def approve(pipe,ordered,renderer,journal_key=None):
    prefix=('quality_critics/'+journal_key+'/') if journal_key else ''
    seats=pipe.router.critic_seats()
    configured=pipe.cfg.critic_seats(pipe.cfg.generator_family())
    if ordered and (not seats or seats!=configured):
        raise ValueError('closure verification requires 100% configured critic coverage')
    models=[identity(s) for s in seats]
    for p in ordered:
        p.editorial['closure_context_hash']=sha(source_material(p,renderer),n=32)
    reviews={p.id:[] for p in ordered}
    errors=[]
    cfg=getattr(pipe,'cfg',None)
    batch_size=min(5,max(1,int(cfg.profile.experiments.get('critic_batch_size',1)))) if cfg else 1
    split_path=pipe.p(prefix+'quality_critic_splits.json')
    split_left=read_json(split_path,{}).get('remaining',2)

    def assess(group,seat,may_split=True):
        nonlocal split_left
        labels={chr(65+i):p for i,p in enumerate(group)}
        packets={label:critic_packet(pipe,p,renderer) for label,p in labels.items()}
        system,user=prompts.build('readiness',items=json.dumps(packets,ensure_ascii=False))
        request_hash=sha(VERSION,system,user,ReadinessBatch.model_json_schema(),identity(seat),n=32)
        try:
            result=pipe.router.review('critic',system,user,ReadinessBatch,spec=seat,
                validator=lambda obj: validate_batch(obj,labels,renderer),
                max_tokens=int(cfg.profile.experiments.get('critic_max_tokens',3072)) if cfg else 3072,
                stage='source_closure',item_id=request_hash)
            for item in result.obj.items:
                item.id=rank._norm_label(item.id)
            if sorted(i.id for i in result.obj.items)!=sorted(labels):
                raise ValueError('missing or duplicate closure labels')
            for item in result.obj.items:
                p=labels[item.id]
                try:
                    view=renderer.evidence(boundary.as_segments(p))
                    enforce_partial_safety(item,view)
                    context=source_material(p,renderer)
                    changes=validate_item(item,p,view,context)
                    rules=enforce_witness_chronology(item,view,context)
                    row=dict(item.model_dump(),**identity(seat),request_hash=request_hash,quote_normalizations=changes)
                    row['rule_normalizations']=rules
                    reviews[p.id].append(row)
                    write_json(pipe.p('quality_readiness_items/'+sha(proof_fingerprint(p),identity(seat),n=32)+'.json'),row)
                except ValueError as exc:
                    errors.append(p.id+': '+str(exc))
        except TruncatedError as exc:
            errors.append('truncated '+request_hash+': '+str(exc))
            if may_split and len(group)==2 and split_left>=2 and getattr(pipe,'use_quality_extra',lambda n:True)(2):
                split_left-=2
                write_json(split_path,{'remaining':split_left})
                for p in group:
                    assess([p],seat,False)
        except BudgetExceeded:
            raise
        except Exception as exc:
            errors.append(type(exc).__name__+': '+str(exc)[:250])

    for seat in seats:
        pending=[]
        for p in ordered:
            row=read_json(pipe.p('quality_readiness_items/'+sha(proof_fingerprint(p),identity(seat),n=32)+'.json'))
            if row:
                try:
                    if {k:row[k] for k in ('model','model_id','family')}!=identity(seat):
                        raise ValueError('changed critic model')
                    item=ReadinessItem.model_validate(row)
                    enforce_partial_safety(item,renderer.evidence(boundary.as_segments(p)))
                    enforce_witness_chronology(item,renderer.evidence(boundary.as_segments(p)),source_material(p,renderer))
                    row.update(item.model_dump())
                    validate_item(item,p,renderer.evidence(boundary.as_segments(p)),source_material(p,renderer))
                    reviews[p.id].append(row)
                    continue
                except (ValueError,KeyError,TypeError):
                    pass
            pending.append(p)
        try:
            for i in range(0,len(pending),batch_size):
                assess(pending[i:i+batch_size],seat)
        except BudgetExceeded as exc:
            # Caller records budget-deferred separately from transport/schema failures.
            raise
    unassessed=[p.id for p in ordered if {r['model'] for r in reviews[p.id]}!={s.label for s in seats}]
    if unassessed:
        pipe.quality_incomplete=True
    for p in ordered:
        p.editorial['readiness']={'fingerprint':proof_fingerprint(p),'models':models,'reviews':reviews[p.id]}
        if not valid_proof(p,renderer,getattr(pipe,'cfg',None)):
            p.status='unassessed_operational' if p.id in unassessed else 'needs_semantic_repair'
    write_json(pipe.p(prefix+'quality_readiness.json'),{'version':VERSION,
        'proofs':{p.id:p.editorial['readiness'] for p in ordered},'unassessed_ids':unassessed,'errors':errors,
        'withheld_ids':[p.id for p in ordered if p.id not in unassessed and not valid_proof(p,renderer)]})
    return [p for p in ordered if valid_proof(p,renderer,getattr(pipe,'cfg',None))]
