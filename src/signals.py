"""S2 — code-only signals: arousal, impact events, dead air, cliff_signal.

Everything here is deterministic and LLM-free (also used by the B1 baseline).
Lexicons were built from the delivery tags and SFX text actually present in the pack
(619 distinct delivery tags; see docs for the survey).
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

from .schemas import Block, Episode

# ---- arousal lexicon (delivery tags of LINE blocks) -------------------------------------------
_HIGH = {
    "screaming", "shouting", "furious", "hysterical", "enraged", "thunderous", "outraged", "piercing",
    "frantic", "panicked", "terrified", "crying out", "booming", "horrified", "wailing", "roaring",
    "shrieking", "yelling", "raging", "unhinged", "agonized", "anguished", "sobbing", "vicious",
    "screams", "explosive", "desperate", "despairing",
}
_MID = {
    "urgent", "threatening", "venomous", "menacing", "sinister", "accusatory", "accusing", "cruel",
    "fierce", "defiant", "pleading", "weeping", "tearful", "heartbroken", "gasping", "shocked", "stunned",
    "incredulous", "indignant", "vindictive", "scornful", "spiteful", "chilling", "begging", "shattered",
    "choked", "alarmed", "deadly", "mocking", "taunting", "gloating", "malicious", "ruthless", "condemning",
    "disgusted", "scathing", "contemptuous", "commanding", "passionate", "intense", "triumphant",
    "ominous", "warning", "ultimatum", "gleeful", "sneering", "dangerous", "unforgiving", "stricken",
}
_LOW = {
    "whispering", "soft", "gentle", "quiet", "calm", "low", "softly", "murmuring", "hushed", "subdued",
    "tender", "warm", "loving", "reflective", "measured", "steady", "resigned", "somber", "intimate",
}


def _tokens(delivery: str) -> List[str]:
    return [t.strip() for t in re.split(r"[,/;]| and ", delivery.lower()) if t.strip()]


def line_arousal(b: Block) -> float:
    """0..1 emotional arousal of a spoken line from delivery tags + punctuation."""
    score = 0.0
    toks = _tokens(b.delivery)
    seen = False
    for t in toks:
        words = set(re.findall(r"[a-z']+", t)) | {t}
        if words & _HIGH:
            score = max(score, 1.0)
            seen = True
        elif words & _MID:
            score = max(score, 0.65)
            seen = True
        elif words & _LOW:
            score = max(score, 0.25)
            seen = True
    if not seen:
        score = 0.4
    txt = b.text.strip()
    if "?!" in txt or "!?" in txt:
        score += 0.2
    elif txt.endswith("!") or "!" in txt:
        score += 0.12
    if txt.endswith(("—", "-", "…", "...")):
        score += 0.08
    return min(1.0, score)


# ---- impact lexicon (ACTION / SHOT / SFX text) -------------------------------------------------
_IMPACT = re.compile(
    r"\b(slap|slaps|slapped|punch|punches|punched|strike|strikes|struck|stab|stabs|stabbed|kick|kicks|"
    r"crash|crashes|crashed|thud|thuds|shatter|shatters|shattered|smash|smashes|slam|slams|slammed|"
    r"burst|bursts|bursting|explod\w*|whip|whips|whipped|lash|lashes|collapse|collapses|collapsed|"
    r"fell|drops|dropped|scream|screams|screamed|gasp|gasps|gasped|shriek\w*|"
    r"lunge|lunges|lunged|shove|shoves|shoved|throw|throws|threw|hurl|hurls|hurled|grab|grabs|grabbed|"
    r"seize|seizes|seized|rip|rips|ripped|tore|drag|drags|dragged|choke|chokes|choked|"
    r"kneel|kneels|knelt|kiss|kisses|kissed|clatter|scuffle|gunshot|brand\w*|"
    r"sear\w*|unsheathe\w*|point(s|ed)? (a|the|his|her)? ?(sword|blade|sabre|dagger|weapon))\b",
    re.I,
)
_LOUD_SFX = re.compile(r"\b(sudden|loud|violent|violently|crash\w*|thud|scream\w*|gasp\w*|shatter\w*|"
                       r"splinter\w*|slam\w*|bang|boom|thunder\w*|crack\w*|clang\w*|slap\w*|punch\w*)\b", re.I)


def impact_score(b: Block) -> float:
    """0..1 'attention event' strength for non-LINE blocks."""
    if b.kind == "LINE":
        return 0.0
    txt = b.text
    if b.kind == "SFX":
        if _LOUD_SFX.search(txt) or _IMPACT.search(txt):
            return 0.85
        return 0.3
    if b.kind == "ACTION":
        return 0.8 if _IMPACT.search(txt) else 0.25
    if b.kind == "SHOT":
        return 0.5 if _IMPACT.search(txt) else 0.1
    return 0.0


def is_event(b: Block) -> bool:
    """Blocks that count as audible/attention events (hook audio, dead-air breaker)."""
    if b.kind == "LINE":
        return True
    return b.kind in ("SFX", "ACTION") and impact_score(b) >= 0.6


def is_sound_event(b: Block) -> bool:
    return is_event(b)


def block_arousal(b: Block) -> float:
    if b.kind == "LINE":
        return line_arousal(b)
    return impact_score(b)


_DIRECTED = re.compile(r'\b(arrest|stop|leave|release|let|give|tell|answer|obey|kill|hang|poison|'
    r'liar|imposter|stole|knew|betray|confess|dare|refuse|must|will|never)\b|[?!]',re.I)
_REACTION = re.compile(r'\b(gasp\w*|recoil\w*|shock\w*|stunn\w*|lunge\w*|strik\w*|struck|'
    r'punch\w*|fight\w*|collaps\w*|reveal\w*|admit\w*|confess\w*|recogniz\w*|'
    r'we will not|i will not|refus\w*|surrender\w*)\b',re.I)


def endpoint_anchors(episode,limit=6):
    """Free high-arousal endpoint proposals. Never semantic approvals."""
    out=[]
    for b in episode.blocks:
        if b.kind not in ('LINE','ACTION') or block_arousal(b)<.6 or not _DIRECTED.search(b.text):
            continue
        reactions=[r for r in episode.blocks if 1.0<=r.t0-b.t1<=8.0
                   and (_REACTION.search(r.text) or impact_score(r)>=.75)]
        if not reactions:
            continue
        r=min(reactions,key=lambda r:(r.t0,-block_arousal(r),r.id))
        if any(v.kind=='LINE' and v.id!=b.id and b.t1<=v.t0<r.t0 for v in episode.blocks):
            continue  # otherwise a later spoken block, not this anchor, would end the cut
        out.append({'cliff_block':b.id,'before_block':r.id,'ep':b.ep,
                    'gap_s':round(r.t0-b.t1,3),'cut_end_s':r.t0,
                    'score':round(block_arousal(b)+block_arousal(r),3)})
    out.sort(key=lambda a:(-a['score'],a['cut_end_s'],a['cliff_block']))
    return out[:limit]


# ---- episode index (shared by cuts, baselines, render) -----------------------------------------
class EpisodeIndex:
    """Precomputed timing structures for one episode."""

    EPS = 0.02

    def __init__(self, ep: Episode):
        self.ep = ep
        self.blocks: List[Block] = ep.blocks
        self.by_id: Dict[str, Block] = {b.id: b for b in ep.blocks}
        self.lines = [b for b in ep.blocks if b.kind == "LINE"]
        self.shots = [b for b in ep.blocks if b.kind == "SHOT"]
        self.events = sorted([b for b in ep.blocks if is_event(b)], key=lambda b: b.t0)
        # merged LINE intervals (overlapping lines form one unit)
        ivs = sorted((b.t0, b.t1) for b in self.lines)
        merged: List[List[float]] = []
        for a, c in ivs:
            if merged and a <= merged[-1][1] + 1e-6:
                merged[-1][1] = max(merged[-1][1], c)
            else:
                merged.append([a, c])
        self.line_ivs: List[Tuple[float, float]] = [(a, c) for a, c in merged]
        self.duration = ep.duration

    # -- point queries --
    def line_active(self, t: float) -> Optional[Tuple[float, float]]:
        """Merged LINE interval strictly containing t (cut would split a line), else None."""
        for a, c in self.line_ivs:
            if a + self.EPS < t < c - self.EPS:
                return (a, c)
        return None

    def is_free(self, t: float) -> bool:
        return self.line_active(t) is None

    def running_shot(self, t: float) -> Optional[Block]:
        best = None
        for s in self.shots:
            if s.t0 <= t + 1e-6 and t < s.t1 - 0.05:
                if best is None or s.t0 > best.t0:
                    best = s
        return best

    def next_event_after(self, t: float) -> float:
        """Earliest event start >= t (LINE start / impact SFX / impact ACTION); inf if none."""
        for b in self.events:
            if b.t0 >= t - 1e-6:
                return b.t0
        return float("inf")

    def boundaries(self) -> List[float]:
        pts = set()
        for b in self.blocks:
            pts.add(round(b.t0, 3))
            pts.add(round(b.t1, 3))
        return sorted(pts)

    def shot_boundaries(self) -> set:
        s = set()
        for b in self.shots:
            s.add(round(b.t0, 3))
            s.add(round(b.t1, 3))
        return s

    def blocks_overlapping(self, t0: float, t1: float) -> List[Block]:
        return [b for b in self.blocks if b.t0 < t1 - 1e-6 and b.t1 > t0 + 1e-6]

    def first_line_start(self) -> float:
        return self.lines[0].t0 if self.lines else float("inf")

    def last_line_end(self) -> float:
        return self.lines[-1].t1 if self.lines else 0.0


# ---- dead air on a concatenated timeline -------------------------------------------------------
def dead_air_max(segments: List[Tuple[EpisodeIndex, float, float]]) -> float:
    """Longest stretch with no LINE/impact event on the concatenated promo timeline.

    segments: [(episode_index, start_s, end_s), ...] in play order.
    """
    spans: List[Tuple[float, float]] = []      # activity intervals on the promo timeline
    off = 0.0
    for idx, s, e in segments:
        for b in idx.blocks:
            if not is_event(b):
                continue
            ea = b.t0
            ec = b.t1 if b.kind == "LINE" else b.t0 + 0.5   # impact events count for ~0.5 s
            if ec <= s or ea >= e:
                continue
            a, c = max(ea, s), min(ec, e)
            spans.append((off + (a - s), off + (c - s)))
        off += e - s
    total = off
    if not spans:
        return total
    spans.sort()
    longest = spans[0][0]            # leading silence
    cur_end = spans[0][1]
    for a, c in spans[1:]:
        if a > cur_end:
            longest = max(longest, a - cur_end)
        cur_end = max(cur_end, c)
    longest = max(longest, total - cur_end)   # trailing silence
    return longest


# ---- cliff_signal (1..5, code-computed) --------------------------------------------------------
_OPEN_CLIFF = {"threat_issued", "impending_harm", "pending_decision", "interrupted_reveal", "payoff_denied",
               "arrival_intrusion"}


def cliff_signal(cliff_block: Block, ep: Episode, cliff_type: str = "") -> float:
    base = 1.0 + 3.0 * block_arousal(cliff_block)
    t = cliff_block.text.strip()
    if t.endswith(("?", "!", "—", "…", "...", "?!", "!?")):
        base += 0.5
    if cliff_type in _OPEN_CLIFF:
        base += 0.5
    if ep.duration and cliff_block.t1 >= ep.duration - 10.0:
        base += 0.5
    return round(min(5.0, base), 2)


def opening_arousal(idx: EpisodeIndex, start_s: float, window: float = 3.0) -> float:
    """Max arousal of events starting within the first `window` s of a promo."""
    best = 0.0
    for b in idx.blocks:
        if is_event(b) and start_s - 1e-6 <= b.t0 < start_s + window:
            best = max(best, block_arousal(b))
    return best


def lines_per_10s(idx: EpisodeIndex, s: float, e: float) -> float:
    n = sum(1 for b in idx.lines if b.t0 >= s - 1e-6 and b.t0 < e)
    return 10.0 * n / max(1e-6, e - s)
