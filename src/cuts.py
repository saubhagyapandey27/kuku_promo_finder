"""S5 — constraint-based cut engine.

The LLM names *blocks*; this module computes the exact cut times and enforces every hard rule.
Design (see plan §5 S5):
  * valid cut point = a time where no spoken line is active (never cut inside a line)
  * promo start   = free point near the hook, with natural-shot and exact-hook options
  * promo end     = never before end_block.t1, extended only inside the running SHOT and never
                    into a block that starts after end_block
  * longest dialogue/impact gap is a soft pacing diagnostic on the promo timeline
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

from rapidfuzz import fuzz

from .schemas import Block, Show, fmt_t
from .signals import EpisodeIndex, dead_air_max, is_event

DEFAULT_PROMO_CFG = dict(
    min_s=20.0, max_s=60.0, target=(25.0, 50.0), max_segments=3, min_segment_s=3.0, min_jump_s=2.0,
    audio_within_s=2.0, hook_start_max_s=2.5, hook_start_pref_s=1.5, lead_in_s=0.3,
    end_hold_max_s=1.5, max_tail_after_cliff_s=3.0, internal_hold_s=0.3, internal_lead_max_s=0.5,
    dead_air_hard_s=6.0, dead_air_soft_s=4.0, quote_min_ratio=85, boundary_clearance_s=0.05,
)


@dataclass
class PromoSpec:
    segments: List[Tuple[str, str]]          # (start_block_id, end_block_id) per segment, play order
    hook_block: str
    cliff_block: str
    hook_quote: str = ""
    cliff_quote: str = ""
    cliff_type: str = ""
    flash_forward: bool = False              # first segment is a flash-forward beat (exempt from chronology)
    check_quotes: bool = True
    check_hook_audio: bool = True            # B1 (episode-ending baseline) disables the audio-first start rule
    start_at_hook: bool = False
    stop_before_block: str = ''              # endpoint-first: exact source reaction onset, half-open


@dataclass
class CutSegment:
    ep: int
    start_s: float
    end_s: float
    start_block: str
    end_block: str
    blocks: List[str] = field(default_factory=list)

    @property
    def dur(self) -> float:
        return self.end_s - self.start_s

    def as_dict(self) -> dict:
        d = asdict(self)
        d["start"] = fmt_t(self.start_s)
        d["end"] = fmt_t(self.end_s)
        return d


@dataclass
class CutResult:
    ok: bool
    segments: List[CutSegment]
    duration: float
    hard: List[str]
    soft: List[str]
    dead_air: float
    info: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return dict(ok=self.ok, segments=[s.as_dict() for s in self.segments],
                    duration=round(self.duration, 2), hard=self.hard, soft=self.soft,
                    dead_air=round(self.dead_air, 2), info=self.info)


class Cutter:
    def __init__(self, show: Show, cfg: Optional[dict] = None):
        self.cfg = {**DEFAULT_PROMO_CFG, **(cfg or {})}
        self.show = show
        self.ix: Dict[int, EpisodeIndex] = {e.ep: EpisodeIndex(e) for e in show.episodes}
        self.blocks: Dict[str, Block] = show.block_index()
        self._sb: Dict[int, set] = {ep: ix.shot_boundaries() for ep, ix in self.ix.items()}

    # ---------------------------------------------------------------- helpers
    def trim_before_block(self,spec,block_id):
        """Propose an earlier whole-line endpoint before a risky visual container."""
        from dataclasses import replace
        block=self.blocks[block_id]
        for index,(a,z) in enumerate(spec.segments):
            first,last=self.blocks[a],self.blocks[z]
            if first.ep!=block.ep or not first.t0<=block.t0<=last.t1:
                continue
            lines=[b for b in self.show.episode(block.ep).blocks if b.kind=='LINE'
                   and first.t0<=b.t0 and b.t1<=block.t0]
            for end in sorted(lines,key=lambda b:b.t1,reverse=True):
                proposal=replace(spec,segments=spec.segments[:index]+[(a,end.id)],
                    cliff_block=end.id,cliff_quote=end.text)
                result=self.cut(proposal)
                if result.ok and result.segments[-1].end_s<=block.t0+.001:
                    return proposal,result
        return None

    def _cover_end(self, ix: EpisodeIndex, t: float) -> float:
        iv = ix.line_active(t)
        return iv[1] if iv else t

    def _is_boundary(self, ep: int, t: float) -> bool:
        return round(t, 3) in self._sb[ep]

    def _candidates(self, ix: EpisodeIndex, lo: float, hi: float, extra: List[float]) -> List[float]:
        pts = {round(lo, 3), round(hi, 3)}
        for p in ix.boundaries():
            if lo - 1e-6 <= p <= hi + 1e-6:
                pts.add(p)
        for b in ix.events:
            for off in (0.0, -0.3):
                p = round(b.t0 + off, 3)
                if lo - 1e-6 <= p <= hi + 1e-6:
                    pts.add(p)
        for p in extra:
            if lo - 1e-6 <= p <= hi + 1e-6:
                pts.add(round(p, 3))
        # midpoints of gaps between merged LINE intervals
        ivs = ix.line_ivs
        for a, c in zip(ivs, ivs[1:]):
            mid = round((a[1] + c[0]) / 2, 3)
            if lo - 1e-6 <= mid <= hi + 1e-6 and c[0] - a[1] > 0.1:
                pts.add(mid)
        return sorted(p for p in pts if p >= 0 and ix.is_free(p))

    # ---------------------------------------------------------------- start of promo
    def snap_start_first(self, spec: PromoSpec, start: Block, hook: Block) -> Tuple[Optional[float], List[str], List[str]]:
        c = self.cfg
        ix = self.ix[start.ep]
        hard: List[str] = []
        soft: List[str] = []
        if hook.ep != start.ep or hook.t0 < start.t0 - 1.0:
            return None, ["hook_before_segment_start"], soft
        lo = max(start.t0 - 1.0, hook.t0 - c["hook_start_max_s"], 0.0)
        hi = hook.t0
        cands = self._candidates(ix, lo, hi, [hook.t0 - c["lead_in_s"], start.t0])
        best, best_score = None, -1e9
        for p in cands:
            nxt = ix.next_event_after(p)
            if spec.start_at_hook and abs(p-hook.t0)>.001:
                continue
            if hook.t0 - p > c["hook_start_max_s"] + 1e-6:
                continue
            gap = (nxt - p) if nxt < 1e8 else 0.3
            score = -abs(gap - c["lead_in_s"]) + 0.5 * self._is_boundary(start.ep, p) \
                - 0.2 * (hook.t0 - p > c["hook_start_pref_s"])
            # prefer not to drop lines of the miner's chosen setup
            if score > best_score:
                best, best_score = p, score
        if best is None:
            hard.append(f"no_valid_start: hook at {fmt_t(hook.t0)} but no free cut point in "
                        f"[{fmt_t(lo)}, {fmt_t(hi)}] with a sound event within {c['audio_within_s']}s")
            return None, hard, soft
        if ix.next_event_after(best)-best > c["audio_within_s"]:
            soft.append("no_early_sound_visual_hook_required")
        if best > start.t0 + 0.05 and any(b.kind == "LINE" and start.t0 - 1e-6 <= b.t0 < best - 1e-6 for b in ix.blocks):
            soft.append("setup_trimmed")
        if not self._is_boundary(start.ep, best):
            soft.append("mid_shot_start")
        if hook.t0 - best > c["hook_start_pref_s"]:
            soft.append("hook_after_1.5s")
        return best, hard, soft

    # ---------------------------------------------------------------- internal segment start
    def snap_start_internal(self, start: Block) -> Tuple[Optional[float], List[str]]:
        c = self.cfg
        ix = self.ix[start.ep]
        soft: List[str] = []
        ev = next((b.t0 for b in ix.events if b.t0 >= start.t0 - 1e-6), None)
        lead = c["internal_lead_max_s"]
        if ev is None or ev <= start.t0 + 1e-6:
            hi, lo = start.t0, start.t0 - lead                 # the start block itself is the first sound
        else:
            hi, lo = ev, max(start.t0 - lead, ev - 1.0)        # silent head: trim forward, never past the sound
        lo = min(max(lo, 0.0), hi)
        cands = self._candidates(ix, lo, hi, [start.t0, hi - c["lead_in_s"]])
        if not cands:
            lo2 = max(start.t0 - 1.5, 0.0)
            cands = self._candidates(ix, lo2, max(hi, start.t0), [start.t0])
            if not cands:
                return None, soft
        best, best_score = None, -1e9
        for p in cands:
            nxt = ix.next_event_after(p)
            gap = (nxt - p) if nxt < 1e8 else 0.3
            score = -0.5 * min(abs(gap - c["lead_in_s"]), 3.0) + 0.5 * self._is_boundary(start.ep, p) \
                - 0.8 * abs(p - start.t0)
            if score > best_score:
                best, best_score = p, score
        nxt = ix.next_event_after(best)
        if nxt - best > 1.0:
            soft.append("slow_after_cut")
        return best, soft

    # ---------------------------------------------------------------- ends
    def snap_end_last(self, end: Block, cliff: Block) -> Tuple[float, List[str]]:
        c = self.cfg
        ix = self.ix[end.ep]
        hard: List[str] = []
        e0 = self._cover_end(ix, end.t1)
        run = ix.running_shot(e0)
        run_left = (run.t1 - e0) if run else 0.3
        nxt = min([b.t0 for b in ix.blocks if b.t0 >= e0 - 1e-6 and b.id != end.id] or [ix.duration + 0.05])
        tail_room = cliff.t1 + c["max_tail_after_cliff_s"] - e0
        hold = max(0.0, min(c["end_hold_max_s"], run_left, nxt - c['boundary_clearance_s'] - e0, tail_room, ix.duration - e0))
        end_s = e0 + hold
        if e0 > cliff.t1 + c["max_tail_after_cliff_s"] + 1e-6:
            hard.append(f"end_too_late_after_cliff: ends {e0 - cliff.t1:.1f}s after the cliff block "
                        f"(max {c['max_tail_after_cliff_s']}s)")
        return end_s, hard

    def snap_end_internal(self, end: Block) -> float:
        c = self.cfg
        ix = self.ix[end.ep]
        e0 = self._cover_end(ix, end.t1)
        run = ix.running_shot(e0)
        run_left = (run.t1 - e0) if run else c["internal_hold_s"]
        nxt = min([b.t0 for b in ix.blocks if b.t0 >= e0 - 1e-6 and b.id != end.id] or [ix.duration + 0.05])
        hold = max(0.0, min(c["internal_hold_s"], run_left, nxt - c['boundary_clearance_s'] - e0, ix.duration - e0))
        return e0 + hold

    # ---------------------------------------------------------------- main
    def cut(self, spec: PromoSpec) -> CutResult:
        c = self.cfg
        hard: List[str] = []
        soft: List[str] = []
        info: dict = {}
        # resolve blocks
        try:
            segs_b = [(self.blocks[a], self.blocks[b]) for a, b in spec.segments]
            hook = self.blocks[spec.hook_block]
            cliff = self.blocks[spec.cliff_block]
        except KeyError as e:
            return CutResult(False, [], 0.0, [f"unknown_block_id: {e.args[0]}"], [], 0.0)
        if not segs_b:
            return CutResult(False, [], 0.0, ["no_segments"], [], 0.0)
        if len(segs_b) > c["max_segments"]:
            hard.append(f"too_many_segments: {len(segs_b)} > {c['max_segments']}")
        # normalise segment order inside an episode (swap if the LLM reversed start/end)
        norm = []
        for s, e in segs_b:
            if s.ep != e.ep:
                hard.append(f"segment_spans_episodes: {s.id}..{e.id}")
                return CutResult(False, [], 0.0, hard, soft, 0.0)
            if e.t1 < s.t0:
                s, e = e, s
            norm.append((s, e))
        # merge tiny or overlapping same-episode neighbours (a < min_jump jump cut is a visible glitch)
        merged: List[Tuple[Block, Block]] = []
        for s, e in norm:
            if merged and merged[-1][0].ep == s.ep and s.t0 - merged[-1][1].t1 < c["min_jump_s"] \
                    and not (spec.flash_forward and len(merged) == 1):
                ps, pe = merged[-1]
                merged[-1] = (ps if ps.t0 <= s.t0 else s, pe if pe.t1 >= e.t1 else e)
                soft.append("merged_adjacent_segments")
            else:
                merged.append((s, e))
        segs_b = merged

        out: List[CutSegment] = []
        for i, (s, e) in enumerate(segs_b):
            first, last = i == 0, i == len(segs_b) - 1
            if first:
                st, h, sf = self.snap_start_first(spec, s, hook)
                hard += h
                soft += sf
            else:
                st, sf = self.snap_start_internal(s)
                soft += [f"seg{i+1}:{x}" for x in sf]
                if st is None:
                    hard.append(f"no_valid_start_seg{i+1}")
            if last:
                en, h = self.snap_end_last(e, cliff)
                hard += h
                if spec.stop_before_block:
                    reaction=self.blocks.get(spec.stop_before_block)
                    if (reaction is None or reaction.ep!=e.ep or e.id!=cliff.id
                            or not 1.0<=reaction.t0-cliff.t1<=8.0 or not self.ix[e.ep].is_free(reaction.t0)):
                        hard.append('invalid_pre_reaction_boundary')
                    else:
                        en=reaction.t0
                        info['stop_before_block']=reaction.id
            else:
                en = self.snap_end_internal(e)
            if st is None:
                continue
            ix = self.ix[s.ep]
            if en - st < c["min_segment_s"] - 1e-6:
                hard.append(f"segment_too_short: seg{i+1} {en - st:.1f}s < {c['min_segment_s']}s")
            seg = CutSegment(ep=s.ep, start_s=round(st, 2), end_s=round(en, 2), start_block=s.id, end_block=e.id,
                             blocks=[b.id for b in ix.blocks_overlapping(st, en)])
            out.append(seg)

        if hard and not out:
            return CutResult(False, [], 0.0, hard, soft, 0.0, info)

        # ----- rules on the final timeline -----
        total = sum(s.dur for s in out)
        if total < c["min_s"] - 1e-6:
            hard.append(f"too_short: {total:.1f}s < {c['min_s']}s")
        if total > c["max_s"] + 1e-6:
            hard.append(f"too_long: {total:.1f}s > {c['max_s']}s")
        if total < c["target"][0] or total > c["target"][1]:
            if c["min_s"] <= total <= c["max_s"]:
                soft.append("outside_25_50s")
        # chronology + jump rule
        for a, b in zip(out, out[1:]):
            if spec.flash_forward and a is out[0]:
                continue
            if (b.ep, b.start_s) < (a.ep, a.end_s - 1e-6):
                hard.append(f"not_chronological: seg ({a.ep},{a.start_s}) then ({b.ep},{b.start_s})")
            elif b.ep == a.ep and b.start_s - a.end_s < c["min_jump_s"] - 1e-6:
                hard.append(f"jump_too_small: {b.start_s - a.end_s:.1f}s < {c['min_jump_s']}s")
        # hook inside the first segment, cliff inside the last
        if out:
            f0, fl = out[0], out[-1]
            if not (hook.ep == f0.ep and f0.start_s - 1e-6 <= hook.t0 <= f0.end_s + 1e-6):
                hard.append("hook_outside_first_segment")
            if not (cliff.ep == fl.ep and fl.start_s - 1e-6 <= cliff.t0 and cliff.t1 <= fl.end_s + 0.06):
                hard.append("cliff_outside_last_segment")
            # nothing spoken after the cliff block (it would answer the question)
            if cliff.ep == fl.ep:
                ixl = self.ix[fl.ep]
                late = [b for b in ixl.lines if b.t0 >= cliff.t1 - 1e-6 and b.t0 < fl.end_s - 1e-6 and b.id != cliff.id]
                if late:
                    hard.append(f"line_after_cliff: {late[0].id}")
            # last segment must not end before the cliff block ends
            if cliff.ep == fl.ep and fl.end_s < cliff.t1 - 0.02:
                hard.append("cliff_cut_off")
        # dead air on the concatenated timeline
        da = dead_air_max([(self.ix[s.ep], s.start_s, s.end_s) for s in out])
        if da > c["dead_air_soft_s"]:
            soft.append(f"dialogue_impact_gap_{da:.1f}s_review_pacing")
        # quotes
        if spec.check_quotes:
            for name, q, b in (("hook", spec.hook_quote, hook), ("cliff", spec.cliff_quote, cliff)):
                if q and fuzz.partial_ratio(q.lower(), b.text.lower()) < c["quote_min_ratio"]:
                    hard.append(f"{name}_quote_mismatch: '{q[:40]}' not in {b.id}")
        # first-sound timing info (hook audio)
        if out:
            ix0 = self.ix[out[0].ep]
            nxt = ix0.next_event_after(out[0].start_s)
            info["first_sound_s"] = round(nxt - out[0].start_s, 2) if nxt < 1e8 else None
            info["hook_at_s"] = round(hook.t0 - out[0].start_s, 2) if hook.ep == out[0].ep else None
        info["segments_after_merge"] = len(out)
        return CutResult(ok=not hard, segments=out, duration=round(total, 2), hard=hard, soft=soft,
                         dead_air=da, info=info)

    def backward_setup_options(self,anchor,limit=4):
        """Hooks 20-58 s before a fixed reaction boundary; at most one real dead-air excision."""
        from .signals import line_arousal,is_event
        end=self.blocks[anchor['cliff_block']]
        ep=self.show.episode(end.ep)
        hooks=[b for b in ep.blocks if b.kind=='LINE' and 20<=anchor['cut_end_s']-b.t0<=58]
        hooks.sort(key=lambda b:(-line_arousal(b),b.t0,b.id))
        out=[]
        for hook in hooks:
            spec=PromoSpec([(hook.id,end.id)],hook.id,end.id,hook.text,end.text,
                           stop_before_block=anchor['before_block'])
            result=self.cut(spec)
            if not result.ok and any(h.startswith('dead_air:') for h in result.hard):
                spoken=[b for b in ep.blocks if b.kind=='LINE' and hook.t0<=b.t0 and b.t1<=end.t1]
                gaps=[(a,b) for a,b in zip(spoken,spoken[1:]) if b.t0-a.t1>6
                      and not any(is_event(v) and v.t0>=a.t1 and v.t0<b.t0 for v in ep.blocks if v.id!=b.id)]
                for a,b in sorted(gaps,key=lambda pair:-(pair[1].t0-pair[0].t1)):
                    candidate=PromoSpec([(hook.id,a.id),(b.id,end.id)],hook.id,end.id,hook.text,end.text,
                                        stop_before_block=anchor['before_block'])
                    trial=self.cut(candidate)
                    if trial.ok:
                        spec,result=candidate,trial
                        break
            if result.ok:
                out.append((spec,result))
            if len(out)>=limit:
                break
        return out
