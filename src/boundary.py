"""Deterministic boundary search, grounded approvals, and coverage-aware scheduling."""
from __future__ import annotations

from collections import defaultdict, deque
import re

from .cutstage import apply_cut, spec_for
from .schemas import FinalViewer
from .util import sha

VERSION = 'question-boundary-2'


def invalidate(p):
    """Any interval change removes all derived claims and approvals, retaining only provenance/controls."""
    p.editorial={k:v for k,v in p.editorial.items() if k in
        ('proposal','required_setup_ids','generation_models','start_at_hook','endpoint_before_block',
         'recovery_rebuilt','rebuilt_setup_from_ids')}
    p.scores,p.judge_models={},{}
    p.label=p.rationale=p.hook_description=p.cliff_description=p.open_question=p.conflict_thread=''
    p.hook_type=p.cliff_type='other'
    p.hook_quote=p.cliff_quote=''


def fingerprint(p, renderer):
    from . import prompts
    from .viewer import VERSION as view_version
    view=renderer.evidence(as_segments(p))
    blocks=renderer.show.block_index()
    context=[blocks[b].model_dump() if b in blocks else {'missing':b}
             for b in p.editorial.get('source_context_ids',[])]
    return sha(VERSION, view_version, view.source,p.editorial.get('source_context_hash'),
               context,
               [(s['ep'], s['start_s'], s['end_s']) for s in p.segments],
               p.hook_block, p.cliff_block, renderer.render(as_segments(p)),
               prompts.load_prompt('final_viewer'), prompts.load_prompt('p7a_hook'),
               prompts.load_prompt('boundary_audit'), FinalViewer.model_json_schema(), n=32)


def as_segments(p):
    from .cuts import CutSegment
    return [CutSegment(ep=s['ep'], start_s=s['start_s'], end_s=s['end_s'],
                       start_block=s['start_block'], end_block=s['end_block']) for s in p.segments]


def round_robin_contexts(promos, blocks, span_s=0):
    groups = defaultdict(list)
    for p in sorted(promos, key=lambda p: (blocks[p.hook_block].ep, blocks[p.hook_block].t0, p.id)
                    if p.hook_block in blocks else (9999, 0, p.id)):
        if p.hook_block in blocks:
            b = blocks[p.hook_block]
            # Overlapping proposals in the same local scene share the source audit.
            # Limit the hook span; do not merge different scenes or a chained entire episode.
            key = next((k for k in reversed(groups) if k[0] == b.ep and k[1] == b.scene
                        and 0 <= b.t0-k[2] <= span_s), None) if span_s else None
            key = key or (b.ep, b.scene, b.t0, b.id)
            groups[key].append(p)
    windows = defaultdict(list)
    for (ep, scene, time, hook), items in groups.items():
        items.sort(key=lambda p: (-p.confidence,p.id))
        windows[(ep-1)//5].append(items)
    queues = {w: deque(sorted(items, key=lambda g: (-max(p.confidence for p in g), g[0].id)))
              for w, items in windows.items()}
    out = []
    while any(queues.values()):
        for w in sorted(queues):
            if queues[w]:
                out.append(queues[w].popleft())
    return out


def contains(p, block):
    return any(s['ep'] == block.ep and s['start_s'] <= block.t0+.02 and s['end_s'] >= block.t1-.02
               for s in p.segments)


def exposed(p, block):
    return any(s['ep'] == block.ep and s['start_s'] < block.t1 and s['end_s'] > block.t0 for s in p.segments)


def feasible_skeletons(group, cutter, limit=10):
    """Offer timing-valid nearby endpoints before asking for semantic selection.

    These are search scaffolds, never approvals. In particular, answered cuts may
    be mechanically legal. A long silent bridge can be removed without guessing
    sub-event times; source and cold assessments still decide causal continuity.
    """
    out, seen = [], set()
    for seed in group[:3]:
        cliff = cutter.blocks.get(seed.cliff_block)
        hook = cutter.blocks.get(seed.hook_block)
        if not cliff or not hook:
            continue
        lines = [b for b in cutter.show.episode(cliff.ep).blocks if b.kind == 'LINE']
        center=min(range(len(lines)),key=lambda i:abs(lines[i].t1-cliff.t1)) if lines else 0
        nearest=sorted(lines[max(0,center-5):center+6],key=lambda b:(abs(b.t1-cliff.t1),b.t0))
        for end in nearest:
            picks = [(seed.hook_block,list(seed.pick[:-1])+[(seed.pick[-1][0],end.id)])] if seed.pick else []
            earlier = [b for b in cutter.show.episode(end.ep).blocks if b.kind=='LINE'
                       and max(0,end.t1-100)<=b.t0<end.t0]
            earlier.sort(key=lambda b:abs(b.t0-hook.t0) if b.ep==hook.ep else end.t0-b.t0)
            picks += [(b.id,[(b.id,end.id)]) for b in earlier]
            found=None
            for opening, parts in picks:
                q=seed.model_copy(deep=True)
                q.hook_block,q.cliff_block=opening,end.id
                q.hook_quote=q.cliff_quote=''
                q.pick=parts
                result=cutter.cut(spec_for(q))
                trials=[parts]
                if any('dead_air' in h for h in result.hard) and len(parts)==1:
                    a,z=parts[0]
                    spoken=[b for b in cutter.show.episode(end.ep).blocks if b.kind=='LINE'
                            and cutter.blocks[a].t0<=b.t0 and b.t1<=end.t1]
                    gaps=sorted(zip(spoken,spoken[1:]),key=lambda pair:pair[1].t0-pair[0].t1,reverse=True)
                    trials += [[(a,left.id),(right.id,z)] for left,right in gaps[:2] if right.t0-left.t1>3]
                for trial in trials:
                    q.pick=trial
                    result=cutter.cut(spec_for(q))
                    if result.ok:
                        found={'hook_block':opening,'end_block':end.id,
                            'segments':[{'start_block':a,'end_block':z} for a,z in trial],
                            'duration_s':round(result.duration,2)}
                        break
                if found:
                    break
            if found:
                key=(found['hook_block'],found['end_block'],str(found['segments']))
                if key not in seen:
                    seen.add(key)
                    out.append(found)
                if len(out)>=limit:
                    return out
    return out


def target_clarification(q, ending, cutter, allowed_ids):
    """A repeated demand can safely retain its intervening target-identifying shot.

    Stay before every known answer, require the same speaker and repeated final
    words, and re-run the entire cut. This proposes a candidate, never approval.
    """
    original=cutter.blocks[q.cliff_block]
    if original.kind!='LINE' or not ending.payoff_ids:
        return None
    words=re.findall(r'\w+',original.text.lower())
    if len(words)<2:
        return None
    future=[cutter.blocks[b] for b in ending.payoff_ids if cutter.blocks[b].ep==original.ep
            and cutter.blocks[b].t0>=original.t1]
    if not future:
        return None
    before=min(b.t0 for b in future)
    names=set(re.findall(r'\b[A-Z][a-z]{3,}\b',ending.question))-{'Will','What','Does','This','That','They','Would','Could'}
    ep=cutter.show.episode(original.ep)
    for b in ep.blocks:
        bw=re.findall(r'\w+',b.text.lower())
        if (b.kind!='LINE' or b.speaker!=original.speaker or b.t0<=original.t1 or b.t1>before
                or bw[-2:]!=words[-2:] or (allowed_ids is not None and b.id not in allowed_ids)):
            continue
        proof=[v for v in ep.blocks if v.kind=='SHOT' and v.t0>=original.t1 and v.t1<=b.t0
               and any(re.search(r'\b'+re.escape(name)+r'\b',v.text) for name in names)]
        if not proof:
            continue
        candidate=q.model_copy(deep=True)
        candidate.id+='-target-'+b.id
        candidate.pick[-1]=(candidate.pick[-1][0],b.id)
        candidate.cliff_block,candidate.cliff_quote=b.id,b.text
        result=cutter.cut(spec_for(candidate))
        apply_cut(candidate,result,cutter.show,cutter)
        if (result.ok and all(contains(candidate,cutter.blocks[x]) for x in ending.required_ids)
                and not any(exposed(candidate,cutter.blocks[x]) for x in ending.payoff_ids)):
            candidate.editorial['target_clarification_ids']=[v.id for v in proof]
            candidate.editorial['audited_pending_anchor']=original.id
            return candidate
    return None


def variants(seed, audit, cutter, allowed_ids=None):
    results, rejects = [], []
    if audit.cannot_support:
        return results, ['source auditor found no supported pending beat']
    ix = cutter.blocks
    endings = {e.id or e.end_block: e for e in audit.endings}
    for template in audit.templates:
        for ending_ref in template.ending_ids:
            ending = endings.get(ending_ref)
            if not ending:
                rejects.append('unknown ending reference: ' + ending_ref)
                continue
            end_id = ending.end_block
            if ending.state not in ('pending', 'causally_reopened'):
                rejects.append('ending already paid off or unclear: ' + end_id)
                continue
            try:
                fact_ids=ending.consequence.action_ids+ending.consequence.target_ids+ending.consequence.stake_ids
                refs = [template.hook_block, end_id] + ending.required_ids + ending.payoff_ids + ending.reopening_ids + template.added_beat_ids+fact_ids
                if any(b not in ix for b in refs):
                    raise ValueError('unknown source evidence ID')
                if allowed_ids is not None and any(b not in allowed_ids for b in refs +
                        [b for s in template.segments for b in (s.start_block, s.end_block)]):
                    raise ValueError('evidence was not in supplied audit context')
                q = seed.model_copy(deep=True)
                q.id = seed.id + '-' + template.id + '-' + end_id
                q.pick = [(s.start_block, s.end_block) for s in template.segments]
                q.pick[-1] = (q.pick[-1][0], end_id)
                q.hook_block, q.cliff_block = template.hook_block, end_id
                q.hook_quote, q.cliff_quote = ix[q.hook_block].text, ix[end_id].text
                q.label, q.open_question, q.cliff_description = template.label, ending.question, ending.question
                q.hook_description, q.conflict_thread = q.hook_quote, ending.question
                q.hook_type, q.cliff_type, q.peak_block = 'other', 'pending_response', ''
                q.missing_context, q.signals, q.base_id = [], {}, seed.id
                q.scores, q.judge_models, q.editorial = {}, {}, {}
                q.flags, q.flash_forward = [], False
                res = cutter.cut(spec_for(q))
                apply_cut(q, res, cutter.show, cutter)
                if not res.ok:
                    raise ValueError('; '.join(res.hard))
                eps = [s['ep'] for s in q.segments]
                if any(b-a > 1 for a, b in zip(eps, eps[1:])):
                    raise ValueError('distant episode contrast disabled')
                missing=[b for b in ending.required_ids+fact_ids if not contains(q,ix[b])]
                if not ending.required_ids or missing:
                    raise ValueError('required causal setup missing or only partly selected: '+str(missing)+
                        '; snapped intervals: '+str([(s['ep'],s['start_s'],s['end_s']) for s in q.segments]))
                if any(exposed(q, ix[b]) for b in ending.payoff_ids):
                    raise ValueError('payoff exposure in final snapped segments')
                if ending.state == 'causally_reopened' and (not ending.reopening_ids or any(not contains(q, ix[b]) for b in ending.reopening_ids)):
                    raise ValueError('linked escalation evidence missing')
                progression = [b for b in dict.fromkeys(template.added_beat_ids)
                               if contains(q, ix[b]) and ix[b].kind in ('LINE', 'ACTION')]
                q.editorial = {'source': ending.model_dump(), 'added_beat_ids': progression,
                               'generation_models': {**seed.editorial.get('generation_models',{}), **seed.judge_models},
                               'audit_unknowns': audit.unknowns}
                results.append(q)
                clarified=target_clarification(q,ending,cutter,allowed_ids)
                if clarified:
                    results.append(clarified)
            except (ValueError, KeyError, IndexError) as exc:
                rejects.append(str(exc))
    # Useful supported progression outranks segment count. No duration bonus or 50-second penalty.
    # Keep a supported original terminal loop before an unrelated newly invented
    # target. Within a loop, useful progression precedes edit complexity.
    results.sort(key=lambda p: (not bool(p.editorial.get('target_clarification_ids')),
                               p.cliff_block != seed.cliff_block,
                               -len(p.editorial['added_beat_ids']), len(p.segments), p.id))
    unique = {}
    for p in results:
        unique.setdefault(tuple((s['ep'], s['start_s'], s['end_s']) for s in p.segments), p)
    return list(unique.values())[:6], rejects


def eligible(p, renderer, cutter):
    e = p.editorial
    if not e.get('assessment') or e.get('fingerprint') != fingerprint(p, renderer):
        return False, 'missing or stale final-cut assessment'
    try:
        a = FinalViewer.model_validate(e['assessment'])
        from .schemas import HookItem
        hook = HookItem.model_validate(e.get('hook_assessment', {}))
        if hook.id != 'H01' or hook.stop_power < 3:
            return False, 'weak or unassessed first-three-second hook'
        current = cutter.cut(spec_for(p))
        if not current.ok or [s.as_dict() for s in current.segments] != p.segments:
            return False, 'final segments differ from validated cut'
        if min(a.cliff,a.clarity,a.pacing)<3 or a.question_kind in ('ordinary_reaction','none'):
            return False, 'insufficient continuation/clarity/pacing or ordinary reaction'
        if a.critical_uncertainty or hook.critical_uncertainty:
            return False, 'critical evidence uncertainty requires review'
        if any(b not in cutter.blocks or not contains(p,cutter.blocks[b]) for b in e.get('required_setup_ids',[])):
            return False, 'required causal setup missing'
        if a.resolved_in_clip:
            return False, 'blind viewer observed the decisive response/emotional payoff'
        view = renderer.evidence(as_segments(p))
        from .readiness import validate_cold
        validate_cold(a,p,view)
        return True, ''
    except (ValueError, KeyError, TypeError):
        return False, 'invalid editorial evidence'


def blind_variants(group,cutter,limit=6):
    """Original from every seed before second alternatives; never mechanically substitute its story question."""
    queues=[]
    for seed in group:
        options=[]
        for free_start in (False,True):
            q=seed.model_copy(deep=True)
            invalidate(q)
            q.editorial['start_at_hook']=free_start
            q.editorial['generation_models']={**seed.editorial.get('generation_models',{}),**seed.judge_models}
            q.id=seed.id+('-free' if free_start else '-natural')
            q.base_id=seed.id
            result=cutter.cut(spec_for(q))
            apply_cut(q,result,cutter.show,cutter)
            if result.ok and all(b in cutter.blocks and contains(q,cutter.blocks[b]) for b in q.editorial.get('required_setup_ids',[])):
                q.hook_quote,q.cliff_quote=cutter.blocks[q.hook_block].text,cutter.blocks[q.cliff_block].text
                if not any(x.segments==q.segments for x in options):
                    options.append(q)
        # Endpoint/backward recovery only when there is no legal local proposal.
        if not options:
            end=cutter.blocks.get(seed.cliff_block)
            if end:
                # Rebuild backward from the SAME proposed ending. Its original causal setup
                # may not fit; explicitly discard that proposed premise and independently
                # reconstruct/verify the new selected cut, preserving the discarded IDs privately.
                anchor={'cliff_block':end.id,'cut_end_s':end.t1,'before_block':''}
                for spec,result in cutter.backward_setup_options(anchor,limit=2):
                    q=seed.model_copy(deep=True)
                    invalidate(q)
                    q.pick=spec.segments; q.hook_block=spec.hook_block
                    q.editorial['rebuilt_setup_from_ids']=q.editorial.pop('required_setup_ids',[])
                    q.editorial['required_setup_ids']=[]
                    q.editorial['recovery_rebuilt']=True
                    q.id=seed.id+'-rebuilt-'+spec.hook_block
                    q.base_id=seed.id
                    apply_cut(q,result,cutter.show,cutter)
                    options.append(q)
        queues.append(options[:2])
    seen=set(); out=[]
    for round in range(2):
        for options in queues:
            if len(options)<=round:
                continue
            q=options[round]
            key=tuple((s['ep'],s['start_s'],s['end_s']) for s in q.segments)
            if key not in seen:
                seen.add(key); out.append(q)
    return out[:limit]


def trim_partial_spoiler(p,cutter):
    for review in p.editorial.get('readiness',{}).get('reviews',[]):
        for issue in review.get('issues',[]):
            if issue['code']!='UNANCHORED_PARTIAL_SHOT_SPOILER':
                continue
            for evidence in issue['evidence']:
                proposed=cutter.trim_before_block(spec_for(p),evidence['block_id'])
                if proposed:
                    spec,result=proposed
                    q=p.model_copy(deep=True)
                    q.id+='-before-'+evidence['block_id']
                    q.pick=spec.segments
                    q.cliff_block,q.cliff_quote=spec.cliff_block,spec.cliff_quote
                    q.editorial={'generation_models':p.editorial.get('generation_models',{}),
                                 'trim_before_block':evidence['block_id']}
                    q.scores,q.judge_models={},{}
                    apply_cut(q,result,cutter.show,cutter)
                    return q
    return None


def no_legal_pre_payoff(p,options,cutter):
    if p is None:
        return not options
    closure={'PAYOFF_ALREADY_SHOWN','EMOTIONAL_STAKES_SETTLED','NO_PENDING_ACTION_AT_CUT'}
    issues=[i for r in p.editorial.get('readiness',{}).get('reviews',[]) for i in r.get('issues',[])
            if i['code'] in closure]
    ids=[e['block_id'] for i in issues for e in i['evidence'] if e['block_id'] in cutter.blocks]
    if not ids:
        return False
    earliest=min((cutter.blocks[b] for b in ids),key=lambda b:(b.ep,b.t0))
    return not any(q.duration_s>=20 and (q.segments[-1]['ep'],q.segments[-1]['end_s'])<=(earliest.ep,earliest.t0)
                   for q in options)


def endpoint_recovery_options(seed,cutter,exclude=()):
    """One bounded deterministic scan; selector sees only these top backward windows."""
    from .signals import endpoint_anchors
    from .render import render_episode_full
    ep=cutter.show.episode(cutter.blocks[seed.hook_block].ep)
    options={}
    windows=[]
    for anchor in endpoint_anchors(ep,limit=3):
        choices=[]
        for spec,result in cutter.backward_setup_options(anchor,limit=4):
            if any([s.as_dict() for s in result.segments]==p.segments for p in exclude):
                continue
            q=seed.model_copy(deep=True)
            q.id=seed.id+'-endpoint-'+anchor['cliff_block']+'-'+sha(spec.segments,n=6)
            q.base_id=seed.id
            q.pick=spec.segments
            q.hook_block,q.cliff_block=spec.hook_block,spec.cliff_block
            q.hook_quote,q.cliff_quote=spec.hook_quote,spec.cliff_quote
            q.editorial={'endpoint_before_block':anchor['before_block'],
                         'generation_models':{**seed.editorial.get('generation_models',{}),**seed.judge_models}}
            q.scores,q.judge_models={},{}
            apply_cut(q,result,cutter.show,cutter)
            key='O'+str(len(options)+1).zfill(2)
            options[key]=q
            choices.append({'option_id':key,'hook_block':q.hook_block,'segments':q.pick,'duration_s':q.duration_s})
        if choices:
            windows.append({'anchor':anchor,'options':choices,
                'backward_source':render_episode_full(ep,t_from=max(0,anchor['cut_end_s']-58),t_to=anchor['cut_end_s'])})
    return options,windows
