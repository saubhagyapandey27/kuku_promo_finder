"""S9 — exports: promos.json / promos.csv in the plan's record shape."""
from __future__ import annotations

import csv
import io
from typing import Dict, List, Optional

from .schemas import Promo, Show, fmt_t


def _at(show: Show, block_id: str) -> str:
    b = show.block_index().get(block_id)
    return f"E{b.ep:02d} {fmt_t(b.t0)}" if b else block_id


def _block_text(show: Show, block_id: str, fallback: str) -> str:
    b = show.block_index().get(block_id)
    return b.text if b else fallback


def segment_str(p: Promo) -> str:
    return " | ".join(f"E{s['ep']:02d} {s['start']}–{s['end']}" for s in p.segments)


def promo_record(p: Promo, rank: int, show: Show, alternates: Optional[List[str]] = None) -> dict:
    sc = p.scores
    from .viewer import ViewerRenderer
    from .scrub import Scrubber, infer_entries
    from .boundary import as_segments
    renderer=ViewerRenderer(show,Scrubber(infer_entries(show)))
    view=renderer.evidence(as_segments(p))
    opening=p.editorial.get('opening_packet',{})
    transcript=[]; offset=0
    for s in p.segments:
        for b in sorted(show.episode(s['ep']).blocks,key=lambda b:(b.t0,b.id)):
            a,z=max(s['start_s'],b.t0),min(s['end_s'],b.t1)
            if a>=z: continue
            complete=b.t0>=s['start_s']-.02 and b.t1<=s['end_s']+.02
            transcript.append({'block_id':b.id,'episode':b.ep,'kind':b.kind,'speaker':b.label if b.kind=='LINE' else '',
                'start_s':round(offset+a-s['start_s'],2),'end_s':round(offset+z-s['start_s'],2),
                'text':b.text if complete else view.content.get(b.id,'[partial visual; see uncertainty]'),
                'timing_basis':view.timing_basis.get(b.id,'partial-visual-uncertain'),
                'uncertainty':view.uncertainty.get(b.id,{})})
        offset+=s['end_s']-s['start_s']
    return {
        "rank": rank, "promo_id": p.id, "title": p.label, "structure": p.structure, "source": p.source,
        "segments": [{"episode": s["ep"], "start": s["start"], "end": s["end"], "start_s": s["start_s"],
                      "end_s": s["end_s"]} for s in p.segments],
        "duration_s": p.duration_s,
        "hook": {"at": _at(show, p.hook_block), "quote": _block_text(show, p.hook_block, p.hook_quote),
                 "type": p.hook_type, "description": p.hook_description},
        "cliffhanger": {"at": _at(show, p.cliff_block), "quote": _block_text(show, p.cliff_block, p.cliff_quote),
                        "type": p.cliff_type, "open_question": p.open_question},
        "conflict_thread": p.conflict_thread,
        "why_it_works": sc.get("why_it_works") or p.rationale,
        "ua_angle": sc.get("ua_angle", ""), "suggested_caption": sc.get("suggested_caption", ""),
        "scores": {k: sc.get(k) for k in ("hook", "cliff_pull", "cliff_signal", "comprehension", "tap", "pacing", "trope",
                                          "intensity", "pre", "composite") if sc.get(k) is not None}
                  | ({"cliff_signal": p.signals.get("cliff_signal")} if "cliff_signal" in p.signals else {}),
        "pl": {"score": sc.get("pl_score"), "ci90": sc.get("pl_ci90"), "rank_range": sc.get("pl_rank_range")}
              if sc.get("pl_score") is not None else None,
        "family_id": p.family_id, "alternates": alternates or [], "flags": p.flags + [f"soft:{s}" for s in p.soft],
        "entry_episode": p.entry_ep, "judge_models": p.judge_models, "status": p.status,
        "wave": sc.get("wave"), "platform_risk": sc.get("platform_risk"), "spoiler_level": sc.get("spoiler_level"),
        "biggest_weakness": sc.get("biggest_weakness", ""), "dead_air_s": p.dead_air_s,
        "repairs": p.repairs, "merged_from": p.merged_from,
        "eligibility": p.editorial,
        "selected_transcript":transcript,
        "selected_transcript_text":'\n'.join(f"[{row['start_s']:.2f}–{row['end_s']:.2f}s] {row['block_id']} {row['kind']} {row['speaker']}: {row['text']}" for row in transcript),
        "opening_window":opening,
        "opening_quote_label":"Opening source line (not necessarily fully audible in three seconds)",
        "longest_dialogue_impact_gap_s":p.dead_air_s,
        "ranking":p.editorial.get('ranking',{}),
        "first_post":sc.get('first_post',False),
    }


def promos_csv(records: List[dict]) -> str:
    buf = io.StringIO()
    cols = ["rank", "promo_id", "wave", "title", "structure", "entry_episode", "segments", "duration_s", "hook_at",
            "hook_quote", "cliff_at", "cliff_quote", "ua_angle", "platform_risk", "composite", "pl_score",
            "why_it_works", "suggested_caption", "flags", "viewer_question", "biggest_weakness", "pacing", "first_post", "selected_transcript"]
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in records:
        w.writerow({
            "rank": r["rank"], "promo_id": r["promo_id"], "wave": r.get("wave") or "", "title": r["title"],
            "structure": r["structure"], "entry_episode": r["entry_episode"],
            "segments": " | ".join(f"E{s['episode']:02d} {s['start']}-{s['end']}" for s in r["segments"]),
            "duration_s": r["duration_s"], "hook_at": r["hook"]["at"], "hook_quote": r["hook"]["quote"],
            "cliff_at": r["cliffhanger"]["at"], "cliff_quote": r["cliffhanger"]["quote"],
            "ua_angle": r["ua_angle"], "platform_risk": r.get("platform_risk") or "",
            "composite": r["scores"].get("composite", r["scores"].get("pre", "")),
            "pl_score": (r["pl"] or {}).get("score", ""), "why_it_works": r["why_it_works"],
            "suggested_caption": r["suggested_caption"], "flags": "; ".join(r["flags"]),
            "viewer_question":r['cliffhanger']['open_question'],"biggest_weakness":r['biggest_weakness'],
            "pacing":r['scores'].get('pacing'),"first_post":r.get('first_post'),
            "selected_transcript":r.get('selected_transcript_text','')})
    return buf.getvalue()
