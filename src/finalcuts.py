"""Opening-first bounded review of distinct proposals, then transparent rubric ranking."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
from . import boundary, prompts, rubric
from .llm.base import BudgetExceeded
from .schemas import FinalViewer, HookBatch, Promo
from .util import read_json, sha, write_json
from .viewer import ViewerRenderer

VERSION='opening-question-rubric-2'


def source_context(show,group,cutter):
    from .render import render_episode_full
    return '\n'.join(render_episode_full(show.episode(ep)) for ep in sorted({cutter.blocks[p.hook_block].ep for p in group}))


def run_final_cuts(pipe):
    from .runner import build_scrubber
    from .readiness import approve,validate_cold,valid_proof
    renderer=ViewerRenderer(pipe.show,build_scrubber(pipe.show,pipe.bible_d))
    pipe.jr=renderer
    if pipe._done('S8_rank'):
        pipe.ranked=pipe._load('S8_rank')
        pipe.promos=pipe.pool=pipe.all_cut=pipe.ranked
        pipe.plan=read_json(pipe.p('08_ranking.json'),{}).get('plan',{})
        pipe.quality_incomplete=not read_json(pipe.p('quality_coverage.json'),{}).get('planned_assessment_complete',False)
        pipe.jury=None
        return pipe.export(pipe.ranked,note='Resumed fingerprint-matched question/rubric result.')

    options=[]; mechanical=[]
    # Each seed represents a distinct proposed story loop. Scene/context grouping cannot erase it.
    groups=boundary.round_robin_contexts(pipe.promos,pipe.cutter.blocks,span_s=0)
    for group in groups:
        rows=boundary.blind_variants(group,pipe.cutter,limit=2*len(group))
        options.extend(rows)
        if not rows:
            mechanical.append({'seed_ids':[p.id for p in group],'status':'no_legal_local_cut'})
    write_json(pipe.p('quality_options.json'),[p.model_dump() for p in options])
    opening_lock=threading.Lock(); opening_results={}
    stop=threading.Event()

    def opening(p):
        packet=renderer.evidence(boundary.as_segments(p),first3s=True)
        hs,hu=prompts.build('p7a_hook',items='H01:\n'+packet.text)
        key=sha(hs,hu,HookBatch.model_json_schema(),n=32)
        # Identical opening content is reviewed once, even for different proposed endings.
        with opening_lock:
            if key not in opening_results:
                opening_results[key]=threading.Event()
                owner=True
            else:
                owner=False
            event=opening_results[key]
        if owner:
            try:
                def validate(obj):
                    if len(obj.items)!=1 or obj.items[0].id!='H01':
                        raise ValueError('one H01 opening required')
                    if any(b not in packet.content and b not in packet.uncertainty for b in obj.items[0].cue_ids):
                        raise ValueError('opening cue IDs must be visible in the first three seconds')
                result=pipe.router.review('hook',hs,hu,HookBatch,max_tokens=1200,
                    stage='opening_screen',item_id=key,validator=validate)
                row={'assessment':result.obj.items[0].model_dump(),'model':result.spec.label,'flags':result.flags}
            except BudgetExceeded as exc:
                stop.set(); row={'status':'budget_deferred','reason':str(exc)}
            except Exception as exc:
                row={'status':'operationally_failed','reason':type(exc).__name__+': '+str(exc)[:250]}
            write_json(pipe.p('quality_openings/'+key+'.json'),row)
            event.set()
        else:
            while not event.wait(.1):
                pipe.router.admission.check_deadline()
            row=read_json(pipe.p('quality_openings/'+key+'.json'))
        if 'assessment' in row:
            p.editorial.update(hook_assessment=row['assessment'],opening_fingerprint=key,
                opening_packet={'text':packet.text,'timing_basis':packet.timing_basis,'uncertainty':packet.uncertainty})
            p.judge_models['hook']=row['model']; p.flags.extend(row['flags'])
        return p,row

    # Keep every decision, including intentional untried variants, separate from editorial rejects.
    screened=[]; opening_states=[]
    def screen(p):
        if stop.is_set():
            return p,{'status':'budget_deferred','reason':'permanent admission stop'}
        try:
            return opening(p)
        except BudgetExceeded as exc:
            stop.set(); return p,{'status':'budget_deferred','reason':str(exc)}
    with ThreadPoolExecutor(max_workers=min(4,max(1,pipe.workers))) as ex:
        for p,row in ex.map(screen,options):
            h=row.get('assessment',{})
            state=row.get('status') or ('promising' if h['stop_power']>=3 and not h['critical_uncertainty'] else 'rejected_opening')
            opening_states.append({'id':p.id,'seed':p.base_id,'status':state,**row})
            if state=='promising':
                screened.append(p)
    write_json(pipe.p('quality_opening_decisions.json'),opening_states)

    # Strong openings first; within a score band give each episode one primary before seconds.
    screened.sort(key=lambda p:(-p.editorial['hook_assessment']['stop_power'],-p.confidence,p.base_id,p.id))
    candidates=[]; used=set(); second=[]
    for p in screened:
        ep=p.entry_ep
        if p.base_id in used:
            continue  # alternative opening is already screened; retain best legal start for the seed
        used.add(p.base_id)
        if any(q.entry_ep==ep and q.editorial['hook_assessment']['stop_power']==p.editorial['hook_assessment']['stop_power'] for q in candidates):
            second.append(p)
        else:
            candidates.append(p)
    candidates+=second
    # Suppress obvious temporal duplicates before expensive full review.
    distinct=[]
    for p in candidates:
        match=next((q for q in distinct if rubric.temporal_iou(p,q)>=.60 and
            q.editorial.get('proposal',{}).get('open_question')==p.editorial.get('proposal',{}).get('open_question')),None)
        if not match:
            distinct.append(p)
    cap=int(pipe.cfg.profile.experiments.get('semantic_limit',40))
    planned=distinct[:cap]
    write_json(pipe.p('quality_planned_review.json'),{'version':VERSION,'cap':cap,'planned_ids':[p.id for p in planned],
        'selection':'opening strength, proposal confidence, one primary per episode within score band; best opening per seed',
        'intentionally_unreviewed_ids':[p.id for p in distinct[cap:]],'frozen_before_dispatch':True})

    def semantic(p):
        path=pipe.p('quality_candidates/'+sha(p.id,p.segments,n=24)+'.json')
        saved=read_json(path)
        if saved:
            q=Promo.model_validate(saved['promo'])
            if saved['status']=='accepted' and not (boundary.eligible(q,renderer,pipe.cutter)[0] and valid_proof(q,renderer,pipe.cfg)):
                raise ValueError('stale final candidate assessment; new namespace required')
            return q,saved['status'],saved.get('reason','')
        status='operationally_failed'; reason=''
        try:
            if stop.is_set():
                raise BudgetExceeded('permanent admission stop')
            pipe.log('Full selected-cut review: '+p.id)
            system,user=prompts.build('final_viewer',clip='FINAL ANCHOR: '+p.cliff_block+'\n'+renderer.render(boundary.as_segments(p)))
            result=pipe.router.review('cold',system,user,FinalViewer,max_tokens=4096,
                stage='final_viewer',item_id=p.id,
                validator=lambda a:validate_cold(a,p,renderer.evidence(boundary.as_segments(p))))
            a=result.obj
            p.editorial.update(assessment=a.model_dump(),viewer_model=result.spec.label)
            p.editorial['fingerprint']=boundary.fingerprint(p,renderer)
            p.judge_models['cold']=result.spec.label; p.flags.extend(result.flags)
            # Presentation is rebuilt exclusively from the final assessments, never seed sales text.
            p.label,p.rationale=a.title,a.why_it_works
            p.open_question,p.cliff_description=a.viewer_question,a.ending_summary
            p.conflict_thread=a.trigger_event+'; '+a.visible_stake
            p.hook_description=p.editorial['hook_assessment']['cue_summary']
            p.hook_quote=pipe.cutter.blocks[p.hook_block].text
            p.cliff_quote=pipe.cutter.blocks[p.cliff_block].text
            p.cliff_type=a.question_kind
            p.scores.update(why_it_works=a.why_it_works,biggest_weakness=a.biggest_weakness)
            if min(a.cliff,a.clarity,a.pacing)<3 or a.resolved_in_clip or a.question_kind in ('ordinary_reaction','none'):
                status='editorially_rejected'; reason='blind continuation/clarity/pacing or question gate'
            elif a.critical_uncertainty:
                status='ambiguous_review'; reason='critical selected-cut evidence uncertainty'
            else:
                ok=bool(approve(pipe,[p],renderer,journal_key=sha(p.id,p.segments,n=24)))
                if p.status=='unassessed_operational':
                    status='operationally_failed'; reason='source critic did not complete valid review'
                elif not ok:
                    status='editorially_rejected'; reason='source critic demonstrated semantic veto'
                else:
                    ok,reason=boundary.eligible(p,renderer,pipe.cutter)
                    status='accepted' if ok else 'editorially_rejected'
                    p.status='final_script_edl' if ok else 'needs_semantic_repair'
        except BudgetExceeded as exc:
            stop.set(); status='budget_deferred'; reason=str(exc)
        except Exception as exc:
            reason=type(exc).__name__+': '+str(exc)[:350]
        write_json(path,{'status':status,'reason':reason,'promo':p.model_dump()})
        return p,status,reason

    accepted=[]; decisions=[]
    with ThreadPoolExecutor(max_workers=min(4,max(1,pipe.workers))) as ex:
        for p,status,reason in ex.map(semantic,planned):
            decisions.append({'id':p.id,'status':status,'reason':reason,'segments':p.segments})
            if status=='accepted': accepted.append(p)
            write_json(pipe.p('quality_decisions.json'),decisions)
    for p in accepted:
        p.family_id='Q'+sha(p.entry_ep,p.cliff_block,p.editorial['assessment']['viewer_question'],n=12)
    if pipe.cfg.profile.ranking.get('mode')=='rubric':
        pipe.ranked=rubric.rank_promos(accepted,pipe.cfg.profile.ranking.get('weights'),pipe.cfg.profile.ranking.get('iou_threshold',.60))
        pipe.plan=rubric.posting_plan(pipe.ranked)
    else:
        from .finalrank import rank_eligible
        pipe.ranked=rank_eligible(pipe,accepted,renderer)
        pipe.plan=rubric.posting_plan(pipe.ranked)
    mining=read_json(pipe.p('mining_coverage.json'),{})
    counts={k:sum(d['status']==k for d in decisions) for k in ('accepted','editorially_rejected','ambiguous_review','operationally_failed','budget_deferred')}
    complete=not any(counts[k] for k in ('operationally_failed','budget_deferred')) and len(decisions)==len(planned)
    pipe.quality_incomplete=bool(mining.get('failed_windows')) or not complete or any(d['status'] in ('operationally_failed','budget_deferred') for d in opening_states)
    coverage={'version':VERSION,'parsed_episodes':[e.ep for e in pipe.show.episodes],
        'mined_episodes':[ep for ep in [e.ep for e in pipe.show.episodes] if not any(ep in f.get('episodes',[]) for f in mining.get('failed_windows',[]))],
        'failed_mining_windows':mining.get('failed_windows',[]),'seed_proposals':len(pipe.promos),
        'boundary_options':len(options),'opening_states':{k:sum(d['status']==k for d in opening_states) for k in {d['status'] for d in opening_states}},
        'planned_semantic_count':len(planned),'planned_semantic_states':counts,'planned_assessment_complete':complete,
        'bounded_search_untried':len(screened)-len(planned),
        'semantically_unreviewed_options':len(options)-len(planned),
        'mechanical_seed_failures':mechanical,
        'accepted_before_suppression':len(accepted),'final_cuts':len(pipe.ranked),'alternates':sum(len(p.editorial.get('alternate_ids',[])) for p in pipe.ranked),
        'episodes_in_final_cuts':sorted({s['ep'] for p in pipe.ranked for s in p.segments}),
        'complete_coverage':not pipe.quality_incomplete,'exhaustive_interval_search':False}
    write_json(pipe.p('quality_coverage.json'),coverage)
    pipe.promos=pipe.pool=pipe.all_cut=pipe.ranked
    pipe.families={p.family_id:[p.id]+p.editorial.get('alternate_ids',[]) for p in pipe.ranked}
    pipe.jury=None
    pipe._save('S8_rank',pipe.ranked); pipe.ck.mark('S8_rank')
    write_json(pipe.p('08_ranking.json'),{'ranking':[p.id for p in pipe.ranked],'plan':pipe.plan,'method':pipe.cfg.profile.ranking})
    return pipe.export(pipe.ranked,note='Opening-first, question-verified screenplay cuts; model rubric, no footage or audience validation.')
