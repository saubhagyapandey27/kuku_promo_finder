"""S3 — window miner (P3). The LLM names block IDs; code computes every timestamp (S5)."""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

from . import prompts
from .bible import arc_text, bible_for_prompt
from .render import render_episode_full
from .schemas import Candidate, MinerOutput, Promo, Show

WINDOW = 5
PREV_TAIL_S = 25.0
NEXT_HEAD_S = 30.0
_ID = re.compile(r"^\s*E?(\d{1,3})\s*[-_ ]\s*B?(\d{1,4})\s*$", re.I)


def norm_id(raw: str, index: Dict[str, object]) -> Optional[str]:
    """'E3-B45' / 'e03-b045' / 'E03-B045' -> 'E03-B045' if it exists, else None."""
    if raw in index:
        return raw
    m = _ID.match(raw or "")
    if not m:
        return None
    cand = f"E{int(m.group(1)):02d}-B{int(m.group(2)):03d}"
    return cand if cand in index else None


def make_windows(eps: Sequence[int], size: int = WINDOW) -> List[List[int]]:
    eps = sorted(eps)
    return [eps[i:i + size] for i in range(0, len(eps), size)]


def structure_of(promo: Promo, show: Show) -> str:
    segs = promo.segments or []
    if promo.flash_forward:
        return "flash_forward"
    if promo.source == "composer":
        return "contrast"
    eps = {s["ep"] for s in segs}
    if len(eps) > 1:
        return "seam_bridge" if promo.source == "miner" else "contrast"
    return "jump_cut" if len(segs) > 1 else "continuous"


def normalise_pick(raw_segments, hook: str, cliff: str, index: Dict[str, object]):
    """-> (pick, hook, cliff, error)"""
    pick: List[Tuple[str, str]] = []
    for s in raw_segments:
        a, b = norm_id(s.start_block, index), norm_id(s.end_block, index)
        if not a or not b:
            return [], "", "", f"unknown_block_id: {s.start_block}..{s.end_block}"
        pick.append((a, b))
    h, c = norm_id(hook, index), norm_id(cliff, index)
    if not h or not c:
        return [], "", "", f"unknown_block_id: hook {hook} / cliff {cliff}"
    return pick, h, c, ""


def candidate_to_promo(c: Candidate, pid: str, window: int, index: Dict[str, object], source: str = "miner", main_eps=None):
    pick, h, cl, err = normalise_pick(c.segments, c.hook_block, c.cliff_block, index)
    if err:
        return None, {"id": pid, "reason": err, "label": c.label}
    refs = c.required_setup_ids + ([c.peak_block] if c.peak_block else [])
    setup=[norm_id(b,index) for b in refs]
    if any(b is None for b in setup) or (main_eps is not None and index[h].ep not in main_eps):
        return None, {"id":pid,"reason":"evidence outside supplied mining packet/main hook window"}
    peak = norm_id(c.peak_block, index) or ""
    return Promo(
        id=pid, source=source, window=window, label=c.label, rationale=c.rationale, pick=pick, hook_block=h,
        cliff_block=cl, peak_block=peak, hook_quote=c.hook_quote, cliff_quote=c.cliff_quote, hook_type=c.hook_type,
        cliff_type=c.cliff_type, hook_description=c.hook_description, cliff_description=c.cliff_description,
        conflict_thread=c.conflict_thread, open_question=c.open_question, missing_context=c.missing_context,
        est_duration_s=c.est_duration_s, confidence=max(0.0, min(1.0, c.confidence)), editorial={"proposal":c.model_dump(), "required_setup_ids":[norm_id(b,index) for b in c.required_setup_ids]}), None


def build_window_prompt(show: Show, bible_d: dict, win: List[int], bible_json: Optional[str] = None):
    first, last = win[0], win[-1]
    prev_ep = show.episode(first - 1)
    nxt_ep = show.episode(last + 1)
    prev_tail = ""
    if prev_ep is not None:
        txt = render_episode_full(prev_ep, t_from=prev_ep.duration - PREV_TAIL_S, header=False)
        prev_tail = f"CONTEXT — PREVIOUS EPISODE TAIL (E{prev_ep.ep}):\n{txt}\n"
    next_head = ""
    if nxt_ep is not None:
        txt = render_episode_full(nxt_ep, t_to=NEXT_HEAD_S, header=False)
        next_head = f"CONTEXT — NEXT EPISODE HEAD (E{nxt_ep.ep}):\n{txt}"
    window_text = "\n\n".join(render_episode_full(show.episode(e)) for e in win if show.episode(e))
    so_far = arc_text(bible_d, [a["episode"] for a in bible_d.get("episode_arc", []) if a["episode"] < first]) or "(start of the series)"
    return prompts.build("p3_miner", bible_json=bible_json or bible_for_prompt(bible_d), story_so_far=so_far,
                         prev_tail=prev_tail, first_ep=first, last_ep=last, window_text=window_text,
                         next_head=next_head)


def mine_window(show: Show, bible_d: dict, win: List[int], widx: int, router, bible_json: Optional[str] = None):
    """-> (promos, drops, router_flags)"""
    system, user = build_window_prompt(show, bible_d, win, bible_json)
    res = router.review("miner", system, user, MinerOutput, stage="S3_mine", item_id=f"W{widx:02d}")
    index={b.id:b for ep in show.episodes for b in ep.blocks
        if ep.ep in win or (ep.ep==win[0]-1 and b.t1>=ep.duration-PREV_TAIL_S)
        or (ep.ep==win[-1]+1 and b.t0<=NEXT_HEAD_S)}
    counts={}
    promos: List[Promo] = []
    drops: List[dict] = []
    for i, c in enumerate(res.obj.candidates, 1):
        pid = f"C{widx:02d}-{i:02d}"
        p, drop = candidate_to_promo(c, pid, widx, index, main_eps=win)
        if p is None:
            drops.append({**drop, "stage": "S3"})
            continue
        ep=index[p.hook_block].ep
        if counts.get(ep,0)>=2:
            drops.append({"id":pid,"reason":"excess_primary_proposals","stage":"S3"})
            continue
        counts[ep]=counts.get(ep,0)+1
        if res.flags:
            p.flags += res.flags
        p.judge_models["miner"] = res.spec.label
        promos.append(p)
    return promos, drops, res.flags


def mine_all(show: Show, bible_d: dict, router, eps: Optional[Sequence[int]] = None, workers: int = 4, log=print):
    wins = make_windows(eps or [e.ep for e in show.episodes])
    bj = bible_for_prompt(bible_d)
    log(f"S3 mining {len(wins)} window(s) of up to {WINDOW} episodes")
    outs = router.map(lambda iw: mine_window(show, bible_d, iw[1], iw[0] + 1, router, bj), list(enumerate(wins)), workers)
    promos: List[Promo] = []
    drops: List[dict] = []
    for (wi, win), out in zip(enumerate(wins), outs):
        if isinstance(out, Exception):
            log(f"  window {wi + 1} (E{win[0]}-E{win[-1]}) failed: {type(out).__name__}: {str(out)[:200]}")
            drops.append({"id": f"W{wi + 1:02d}", "reason": f"window_failed: {out}", "stage": "S3", "episodes":win})
            continue
        p, d, _ = out
        promos += p
        drops += d
    return promos, drops
