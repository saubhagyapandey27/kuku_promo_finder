"""Renderers.

generator-facing:  render_episode_full / render_episode_digest / render_local   (real names, block IDs)
judge-facing:      JudgeRenderer.render(...)   (promo-relative times, scrubbed, ON SCREEN header, [CUT] marks)
                   JudgeRenderer.render(..., first3s=True) for the hook test
"""
from __future__ import annotations

import math
import re
from typing import List, Optional, Sequence

from .cuts import CutSegment
from .schemas import Block, Episode, Show
from .scrub import Scrubber, strip_impact_clauses, truncate_words
from .signals import EpisodeIndex

HEADER_MAX_WORDS = 35

# Container prose has no word-level timing. Only contemporaneous setup/framing
# is projected; possible result clauses remain separate even when the camera is shared.
_RESULT_CLAUSE = re.compile(r'\b(arrests|arrested|arresting|arrest(?=\s+(?:him|her|them|V\d+|the))|'
    r'releas\w*|uncuff\w*|handcuff(?:s|ed|ing)?(?=\s+(?:him|her|them|V\d+|the))|freed|frees|rescued?|'
    r'kills?|killed|killing|shoot\w*|fires|fired|firing|strik\w*|struck|knock\w*|collaps\w*|dead|defeat\w*|'
    r'reveal\w*|confirm\w*|recogniz\w*|accept\w*|surrender\w*|forgiv\w*|exonerat\w*|'
    r'succeed\w*|relief|resign\w*|settled|resolv\w*)\b',re.I)
_FUTURE_CLAUSE = re.compile(r'\b(will|would|might|could|may|about to|going to|orders?|demands?|threaten\w*)\b',re.I)
_SETUP_STATE = re.compile(r'\b(stands?|standing|holds?|holding|points?|pointing|aims?|aiming|faces?|facing|'
    r'rests?|resting|surrounded|flanked)\b',re.I)
_CAMERA_PREFIX = re.compile(r'^(?:(?:extreme |tight |medium |wide |high |low[- ]angle |tracking |profile |symmetrical )*'
    r'(?:close[- ]?up|shot|two[- ]shot|three[- ]shot|full shot)|wide shot|medium shot)\b',re.I)


def partial_visual_projection(block,episode,start,end):
    """Return exact source clauses and independently timed LINE/SFX anchors.

    Temporal overlap licenses camera/setup context, never an unanchored result.
    No word-count interpolation is used for visual actions.
    """
    anchors=[b for b in episode.blocks if b.kind in ('LINE','SFX')
             and b.t0>=start-.02 and b.t1<=end+.02 and b.t1>block.t0 and b.t0<block.t1]
    anchored,unknown=[],[]
    clauses=[c.strip() for c in re.split(r'(?<=[.;])\s+|\s+(?=then\b)|,\s*(?=(?:then|after|before)\b)',block.text,flags=re.I) if c.strip()]
    for clause in clauses:
        result=bool(_RESULT_CLAUSE.search(clause) and not _FUTURE_CLAUSE.search(clause))
        camera=bool(_CAMERA_PREFIX.match(clause))
        # A neutral camera/setup description can be anchored by simultaneous speech/sound.
        if (camera or _SETUP_STATE.search(clause)) and not result and not re.match(r'^(then|after|before)\b',clause,re.I):
            anchored.append({'quote':clause,'anchor_ids':[b.id for b in anchors]})
        elif anchors and any(b.text.strip() in clause and b.text.strip() for b in anchors) and not result:
            anchored.append({'quote':clause,'anchor_ids':[b.id for b in anchors if b.text.strip() in clause]})
        else:
            # Preserve a neutral framing prefix without asserting the following result.
            if camera and result:
                match=re.search(r'\s+(?=(?:as|then|who|arrests?|releases?|fires?|shoots?|collapses?|reveals?)\b)',clause,re.I)
                if match:
                    prefix=clause[:match.start()].strip(' ,')
                    if len(prefix.split())>=4 and prefix.split()[-1].lower() not in ('on','of','at','the','a'):
                        anchored.append({'quote':prefix,'anchor_ids':[b.id for b in anchors]})
                        clause=clause[match.end():].strip()
            unknown.append({'quote':clause,'possible_payoff':result})
    return {'anchored_clauses':anchored,'unanchored_clauses':unknown}


def _ts(t: float) -> str:
    m, s = divmod(max(0.0, t), 60)
    return f"{int(m):02d}:{s:04.1f}"


def _trim_words(text: str, n: int) -> str:
    w = text.split()
    return text if len(w) <= n else " ".join(w[:n]) + "…"


# =========================================================================== generator-facing
def _line_str(b: Block, digest: bool) -> str:
    stamp = f"[{b.id} {_ts(b.t0)}–{_ts(b.t1)}]"
    if b.kind == "LINE":
        return f'{stamp} LINE {b.label}: "{b.text}"'
    text = _trim_words(b.text, 15) if (digest and b.kind == "SHOT") else b.text
    tag = {"SHOT": "SHOT", "ACTION": "ACTION", "SFX": "SFX"}[b.kind]
    return f"{stamp} {tag}: {text}"


def render_episode(ep: Episode, digest: bool = False, t_from: float = 0.0, t_to: float = 1e9,
                   header: bool = True) -> str:
    out: List[str] = []
    if header:
        out.append(f"=== E{ep.ep:02d} (duration {_ts(ep.duration)}) — logline: {ep.logline} ===")
    cur = -1
    for b in ep.blocks:
        if b.t1 < t_from or b.t0 > t_to:
            continue
        if b.scene != cur:
            cur = b.scene
            sc = ep.scenes[cur] if cur < len(ep.scenes) else None
            if sc:
                out.append(f"-- SCENE {cur + 1}: {sc.heading} ({_ts(sc.start)}–{_ts(sc.end)}) --")
        out.append(_line_str(b, digest))
    return "\n".join(out)


def render_episode_full(ep: Episode, **kw) -> str:
    return render_episode(ep, digest=False, **kw)


def render_episode_digest(ep: Episode, **kw) -> str:
    return render_episode(ep, digest=True, **kw)


def render_compact(show: Show, eps: Optional[Sequence[int]] = None, shot_words: int = 15) -> str:
    """ID-free, time-free digest of the whole script (bible call): ~15% fewer tokens than the ID form."""
    out: List[str] = []
    for ep in show.episodes:
        if eps is not None and ep.ep not in eps:
            continue
        out.append(f"=== E{ep.ep:02d} ===")
        for b in ep.blocks:
            if b.kind == "LINE":
                out.append(f'{b.label}: "{b.text}"')
            else:
                t = _trim_words(b.text, shot_words) if b.kind == "SHOT" else b.text
                out.append(f"{b.kind}: {t}")
    return "\n".join(out)


def render_local(show: Show, ep_no: int, t0: float, t1: float, pad: float = 20.0) -> str:
    ep = show.episode(ep_no)
    return render_episode(ep, digest=False, t_from=t0 - pad, t_to=t1 + pad, header=False)


# =========================================================================== judge-facing
def humanize_heading(h: str) -> str:
    """'INT. CATHEDRAL – CONTINUOUS' -> 'interior, cathedral' ; 'EXT. WOODLAND PATH – NIGHT' -> 'exterior, woodland path, night'"""
    h = (h or "").strip()
    m = re.match(r"^(INT|EXT|INT/EXT|EXT/INT)\.?\s*(.*)$", h, re.I)
    place_part = h
    kind = ""
    if m:
        kind = {"INT": "interior", "EXT": "exterior"}.get(m.group(1).upper(), "interior/exterior")
        place_part = m.group(2)
    bits = re.split(r"\s*[–—-]\s*", place_part)
    place = bits[0].strip().lower()
    extras = [b.strip().lower() for b in bits[1:] if b.strip().lower() not in ("continuous", "later", "moments later", "same")]
    return ", ".join(x for x in [kind, place] + extras if x)


class JudgeRenderer:
    """Builds the viewer-facing text of a promo from its CutSegments."""

    def __init__(self, show: Show, scrubber: Optional[Scrubber] = None, indexes: Optional[dict] = None):
        self.show = show
        self.scrub = scrubber or Scrubber()
        self.ix = indexes or {e.ep: EpisodeIndex(e) for e in show.episodes}

    # ------------------------------------------------------------------ pieces
    def _on_screen(self, ep: Episode, seg_start: float, first_block_shot: Optional[Block]) -> str:
        """Deterministic setting header: scene heading + the scene's establishing SHOT if it ended before the cut.
        Never contains blocks that happened just before the cut (so no pre-cut slap leaks in)."""
        scene_idx = None
        for b in ep.blocks:
            if b.t1 > seg_start - 1e-6:
                scene_idx = b.scene
                break
        if scene_idx is None:
            scene_idx = ep.blocks[-1].scene
        sc = ep.scenes[scene_idx] if scene_idx < len(ep.scenes) else None
        setting = humanize_heading(sc.heading) if sc else ""
        est = ""
        shots = [b for b in ep.blocks if b.scene == scene_idx and b.kind == "SHOT"]
        if shots:
            s0 = shots[0]
            if s0.t1 <= seg_start + 1e-6 and not (first_block_shot and first_block_shot.id == s0.id):
                est = strip_impact_clauses(self.scrub.scrub(s0.text, ep.ep))
        text = f"setting: {setting}" if setting else ""
        if est:
            text = (text + "; " if text else "") + est
        words = text.split()
        if len(words) > HEADER_MAX_WORDS:
            text = " ".join(words[:HEADER_MAX_WORDS]) + "…"
        return text

    def _fmt_block(self, b: Block, ep_no: int, rel_t: float, in_progress: bool, crop_words: Optional[int] = None) -> str:
        ts = f"{rel_t:4.1f}s"
        if b.kind == "LINE":
            who = self.scrub.speaker(b.speaker, ep_no)
            if re.search(r"\((?:O\.S\.|OS|V\.O\.|VO|OFF-?SCREEN)\)", b.label, re.I):
                who += " (off-screen)"
            deliv = self.scrub.scrub(b.delivery, ep_no) if b.delivery else ""
            text = b.text
            if crop_words is not None:
                w = text.split()
                text = " ".join(w[:crop_words]) + "…"
            d = f" ({deliv})" if deliv else ""
            return f'{ts}  {who}{d}: "{text}"'
        body = self.scrub.scrub(b.text, ep_no)
        tag = {"SHOT": "SHOT", "ACTION": "ACTION", "SFX": "SOUND"}[b.kind]
        prog = " (in progress)" if in_progress else ""
        return f"{ts}  {tag}{prog}: {body}"

    # ------------------------------------------------------------------ public
    @staticmethod
    def _scene_at(ep: Episode, t: float):
        for b in ep.blocks:
            if b.t1 > t - 1e-6:
                return b.scene
        return None

    def render(self, segments: Sequence[CutSegment], first3s: bool = False, headers: bool = True) -> str:
        out: List[str] = []
        offset = 0.0
        limit = 3.0 if first3s else 1e9
        for si, seg in enumerate(segments):
            if si > 0:
                if offset >= limit:
                    break
                out.append("[CUT]")
            ep = self.show.episode(seg.ep)
            ix = self.ix[seg.ep]
            s, e = seg.start_s, seg.end_s
            run_shot = ix.running_shot(s + 1e-6)
            running = [run_shot] if run_shot is not None and run_shot.t0 < s - 1e-6 else []
            # the setting header is for a viewer who has not seen the place yet: a jump cut inside the same scene
            # keeps the same setting, and repeating the establishing shot made judges read it as a replayed moment
            same_scene_as_prev = (si > 0 and segments[si - 1].ep == seg.ep
                                  and self._scene_at(ep, segments[si - 1].start_s) == self._scene_at(ep, s))
            if headers and not same_scene_as_prev:
                out.append("ON SCREEN (already established): " + (self._on_screen(ep, s, run_shot) or "(no setting given)"))
            for b in ep.blocks:
                if b.t1 <= s + 1e-6 or b.t0 >= e - 1e-6:
                    continue
                started_before = b.t0 < s - 1e-6
                if started_before:
                    if b.kind == "SHOT":
                        if not running or b.id != running[0].id:
                            continue
                    elif b.kind == "ACTION":
                        if (b.t1 - s) < 0.5 * max(1e-6, b.t1 - b.t0):
                            continue
                    else:
                        continue            # SFX/LINE that started before the cut already played
                rel = offset + max(0.0, b.t0 - s)
                if rel >= limit:
                    continue
                crop = None
                if first3s and b.kind == "LINE" and offset + (b.t1 - s) > limit:
                    words = b.text.split()
                    frac = (limit - rel) / max(1e-6, b.t1 - b.t0)
                    n = int(math.floor(len(words) * frac))
                    if n <= 0:
                        continue
                    crop = n
                out.append(self._fmt_block(b, seg.ep, rel, started_before, crop))
            offset += e - s
        return "\n".join(out)
