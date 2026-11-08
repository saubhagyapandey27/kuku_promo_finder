"""report.md — a readable summary of one run (what the editor reads first)."""
from __future__ import annotations

import json
from collections import Counter
from typing import List, Optional

from .export import segment_str
from .schemas import Promo


def _drop_reasons(pipe) -> Counter:
    c: Counter = Counter()
    for d in pipe.drops:
        reason = d.get("reason", "") or ""
        key = reason.split(":")[0].strip() or "unknown"
        c[f"{d.get('stage', '?')}: {key}"] += 1
    return c


def make_report(pipe, recs: List[dict]) -> str:
    if pipe.cfg.profile.ranking.get('mode')=='rubric':
        from .util import read_json
        coverage=read_json(pipe.p('quality_coverage.json'),{})
        cost=pipe.router.cost_summary()
        rows=['# Run review', '', f'Parsed episodes: {len(pipe.show.episodes)}. Exported primary cuts: {len(recs)}.',
            'Ranking: 20 × (0.35 H + 0.30 T + 0.20 C + 0.15 P); model editorial heuristics, not audience evidence.',
            '', 'Coverage:', json.dumps(coverage,ensure_ascii=False,indent=2), '',
            'Posting order: '+', '.join(getattr(pipe,'plan',{}).get('posting_order',[])),
            'Strong first-post shortlist: '+', '.join(getattr(pipe,'plan',{}).get('first_post_shortlist',[])),
            '', 'Costs (actual request receipts and conservative ledger scope):',json.dumps(cost,indent=2),
            '', 'No footage alignment, independent human accuracy or conversion measurement.']
        for r in recs:
            rows += ['',f"## {r['rank']} {r['title']}",r['selected_transcript_text'],
                'Open question: '+r['cliffhanger']['open_question'],'Concern: '+r['biggest_weakness']]
        return '\n'.join(rows)+'\n'
    show = pipe.show
    cfg = pipe.cfg
    r = pipe.router
    cs = r.cost_summary()
    lines: List[str] = []
    A = lines.append
    A(f"# Promo Finder report — {show.title}")
    A("")
    A(f"- **Profile:** `{cfg.profile.name}` — {cfg.profile.description}")
    A(f"- **Episodes:** {len(show.episodes)} ({show.episodes[0].ep}–{show.episodes[-1].ep}), "
      f"{sum(1 for e in show.episodes for b in e.blocks if b.kind == 'LINE')} dialogue lines")
    A(f"- **Cost:** ${cs['total_usd']:.3f} ({sum(cs['calls'].values())} model calls, {cs['cache_hits']} cache hits); "
      f"wall time {cs.get('elapsed_s', 0):.0f}s" if 'elapsed_s' in cs else
      f"- **Cost:** ${cs['total_usd']:.3f} ({sum(cs['calls'].values())} model calls, {cs['cache_hits']} cache hits)")
    A("")
    A("## Models by stage")
    A("")
    A("| Stage | Model |")
    A("|---|---|")
    seats = sorted({m for st, m in r.stage_models.items() if st.startswith("S8_rank:")})
    for st, m in sorted(r.stage_models.items()):
        if not st.startswith("S8_rank:"):
            A(f"| {st} | `{m}` |")
    if seats:
        A(f"| S8 jury seats (listwise, one model per seat) | {', '.join(f'`{m}`' for m in seats)} |")
    A(f"| generator (bible/miner/repair) | `{pipe.bible_meta.get('model', '?')}` (+ fallbacks flagged per candidate) |")
    if cfg.profile.allow_same_family:
        A("")
        A("> **Caveat:** this profile judges with the same model family that generated the candidates; "
          "scores are likely optimistic (self-preference).")
    A("")
    A("## Funnel")
    A("")
    mined = len(pipe.promos_mined) if hasattr(pipe, "promos_mined") else None
    A(f"- candidates after dedup: **{len(pipe.promos)}** in **{len(pipe.families)}** families")
    A(f"- judged pool: **{len(pipe.pool)}**; jury ranked: **{len((getattr(pipe, 'jury', None) or {}).get('ranking', []))}**")
    dr = _drop_reasons(pipe)
    if dr:
        A("- dropped: " + ", ".join(f"{k} ×{v}" for k, v in dr.most_common(8)))
    A("")
    plan = getattr(pipe, "plan", None)
    if plan:
        A("## Posting plan")
        A("")
        by_id = {x["promo_id"]: x for x in recs}

        def row(pid):
            x = by_id.get(pid)
            if not x:
                return ""
            return (f"- **{pid}** (#{x['rank']}) {x['duration_s']:.0f}s — {x['title']} — "
                    f"`{x['ua_angle'] or 'n/a'}` — {segment_str_rec(x)}")
        A("**Wave 1 — creative test (best promo per acquisition angle)**")
        A("")
        for pid in plan["wave1"]:
            A(row(pid))
        A("")
        A("**Wave 2 — next best overall (≤1 per family and scene)**")
        A("")
        for pid in plan["wave2"]:
            A(row(pid))
        if plan.get("refresh_alternates"):
            A("")
            A("**Refresh alternates (same family as a wave-1 winner):** " + ", ".join(plan["refresh_alternates"]))
        if plan.get("human_review"):
            A("")
            A("**Human review (platform-risk or moderation signal):** " + ", ".join(plan["human_review"]))
        A("")
    A("## Top promos")
    A("")
    for x in recs[:15]:
        pl = x.get("pl") or {}
        A(f"### #{x['rank']} {x['title']}  `{x['promo_id']}`  · {x['structure']} · {x['duration_s']:.0f}s")
        A("")
        A(f"- **Cut:** {segment_str_rec(x)}")
        A(f"- **Hook** ({x['hook']['type']}, at {x['hook']['at']}): “{x['hook']['quote'][:140]}”")
        A(f"- **Cliffhanger** ({x['cliffhanger']['type']}, at {x['cliffhanger']['at']}): “{x['cliffhanger']['quote'][:140]}” "
          f"— {x['cliffhanger']['open_question']}")
        A(f"- **Why it works:** {x['why_it_works']}")
        sc = x["scores"]
        detail=f"; PL {pl['score']}" if pl.get('score') is not None else ''
        if pl.get('rank_range'):
            detail+=f" (rank range {pl['rank_range'][0]}–{pl['rank_range'][1]})"
        A("- **Scores:** " + ", ".join(f"{k} {v}" for k, v in sc.items() if v is not None) + detail)
        if x.get("biggest_weakness"):
            A(f"- **Weakness:** {x['biggest_weakness']}")
        if x["flags"]:
            A(f"- **Flags:** {', '.join(x['flags'])}")
        A("")
    jury = getattr(pipe, "jury", None)
    if jury:
        A("## Jury diagnostics")
        A("")
        A(f"- seats: {', '.join(jury['seats_used'])}; rankings: {len(jury['calls'])}")
        if jury["agreement"]:
            A("- seat agreement (Kendall τ): " + ", ".join(f"{k}: {v}" for k, v in jury["agreement"].items()))
        pe = jury["position"]
        if pe.get("rho") is not None:
            A(f"- position effect (Spearman, display slot vs rank; 0 = none): ρ={pe['rho']} (n={pe['n']})")
        A("- CIs are conditional on the adaptive R3 grouping (round-stratified ranking bootstrap).")
        A("")
    A("## Flags worth a human look")
    A("")
    mod = 0
    try:
        with open(pipe.p("moderation_hits.jsonl"), encoding="utf-8") as f:
            mod = sum(1 for _ in f)
    except OSError:
        pass
    A(f"- moderation blocks (a moderated API refused the scene): {mod} — also a likely ad-platform signal")
    bm = pipe.bible_meta
    if bm.get("flags"):
        A(f"- bible flags: {', '.join(bm['flags'])}; label resolution {bm.get('label_resolution', 0):.0%}")
    A("- screenplay timestamps have not been aligned with produced footage; edited cuts require reassessment.")
    return "\n".join(lines) + "\n"


def segment_str_rec(x: dict) -> str:
    return " | ".join(f"E{s['episode']:02d} {s['start']}–{s['end']}" for s in x["segments"])
