"""Grouping, label validation, Plackett-Luce fitting and posting plans for verified cuts."""

from __future__ import annotations

import itertools

import random

import re

import string

from collections import defaultdict

from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import prompts

from .cuts import CutSegment

from .render import JudgeRenderer

from .schemas import ListwiseRanking, Promo

from .util import PersistentRateLimit

GROUP = 6

ALPHA = 0.01

N_BOOT = 200

def snake_groups(ids: Sequence[str], k: int = 3) -> List[List[str]]:
    """ids ordered best->worst; snake assignment into k groups (R1)."""
    groups: List[List[str]] = [[] for _ in range(k)]
    for i, pid in enumerate(ids):
        r, c = divmod(i, k)
        groups[c if r % 2 == 0 else k - 1 - c].append(pid)
    return groups

def group_sizes(n: int, max_size: int = GROUP) -> List[int]:
    """n items -> equal-as-possible groups of at most `max_size` (24 -> 6,6,6,6 ; 21 -> 6,5,5,5)."""
    k = max(1, -(-n // max_size))
    base, extra = divmod(n, k)
    return [base + (1 if i < extra else 0) for i in range(k)]

def rotated_groups(ids: Sequence[str], prior: List[List[str]], k: int = 3, seed: int = 11, tries: int = 120,
                   sizes: Optional[Sequence[int]] = None) -> List[List[str]]:
    """Partition into groups (k equal ones, or the given sizes) minimising how many pairs were already together in
    `prior` (random restarts + swap local search; swaps keep group sizes)."""
    seen: Dict[Tuple[str, str], int] = defaultdict(int)
    for g in prior:
        for a, b in itertools.combinations(sorted(g), 2):
            seen[(a, b)] += 1
    sizes = list(sizes) if sizes else [len(ids) // k] * k
    k = len(sizes)
    rng = random.Random(seed)

    def cost(gs):
        return sum(seen[(min(a, b), max(a, b))] for g in gs for a, b in itertools.combinations(g, 2))

    best, best_cost = None, 1e9
    for _ in range(tries):
        order = list(ids)
        rng.shuffle(order)
        groups, o = [], 0
        for s in sizes:
            groups.append(order[o:o + s])
            o += s
        c0 = cost(groups)
        improved = True
        while improved and c0 > 0:
            improved = False
            for gi, gj in itertools.combinations(range(k), 2):
                for x in range(len(groups[gi])):
                    for y in range(len(groups[gj])):
                        groups[gi][x], groups[gj][y] = groups[gj][y], groups[gi][x]
                        c1 = cost(groups)
                        if c1 < c0:
                            c0, improved = c1, True
                        else:
                            groups[gi][x], groups[gj][y] = groups[gj][y], groups[gi][x]
        if c0 < best_cost:
            best, best_cost = [list(g) for g in groups], c0
            if best_cost == 0:
                break
    return best

def fit_pl(items: Sequence[str], rankings: List[List[str]], alpha: float = ALPHA) -> Dict[str, float]:
    """Plackett-Luce log-strengths via choix (I-LSR with a small Dirichlet-style prior; absent items -> prior)."""
    import choix
    idx = {pid: i for i, pid in enumerate(items)}
    data = [[idx[x] for x in r if x in idx] for r in rankings if len([x for x in r if x in idx]) >= 2]
    if not data:
        return {pid: 0.0 for pid in items}
    params = choix.ilsr_rankings(len(items), data, alpha=alpha)
    params = np.asarray(params) - float(np.mean(params))
    return {pid: float(params[i]) for pid, i in idx.items()}

_LABEL = re.compile(r"\b([A-Z])\b")

def _norm_label(x: str) -> str:
    t = x.strip().upper().replace("PROMO", " ").replace('_',' ').replace('-',' ')
    m = _LABEL.search(t)
    return m.group(1) if m else t.strip()[:1]

def _valid_ranking(r: ListwiseRanking, labels: Dict[str, str]) -> Optional[List[str]]:
    """Map the model's ranking (labels) to promo ids; None unless it lists every label exactly once."""
    got = [_norm_label(x) for x in r.ranking]
    if sorted(got) != sorted(labels):
        return None
    return [labels[g] for g in got]

def posting_plan(ranked: List[Promo], show, angles: Sequence[str], max_wave2: int = 10) -> dict:
    """Wave 1 = best promo per acquisition angle; wave 2 = next best overall (<=1 per family, <=1 per scene);
    alternates of winning angles are refresh candidates; platform_risk=high goes to a human-review list."""
    idx = show.block_index()

    def scene_key(p: Promo):
        b = idx.get(p.segments[0]["start_block"]) if p.segments else None
        return (b.ep, b.scene) if b else (p.entry_ep, -1)

    wave1: List[Promo] = []
    used_angles = set()
    for p in ranked:
        a = (p.scores.get("ua_angle") or "").strip().lower()
        if a and a not in used_angles and p.scores.get("platform_risk") != "high":
            used_angles.add(a)
            wave1.append(p)
    fams = {p.family_id for p in wave1}
    scenes = {scene_key(p) for p in wave1}
    wave2: List[Promo] = []
    cand = [p for p in ranked if p not in wave1 and p.scores.get("platform_risk") != "high"]
    # early-funnel promos (entry ep <= 10) break near-ties: stable sort keeps PL order otherwise
    for p in cand:
        if len(wave1) + len(wave2) >= max_wave2:
            break
        if p.family_id in fams or scene_key(p) in scenes:
            continue
        fams.add(p.family_id)
        scenes.add(scene_key(p))
        wave2.append(p)
    for p in wave1:
        p.scores["wave"] = 1
    for p in wave2:
        p.scores["wave"] = 2
    refresh = [p.id for p in ranked if p not in wave1 and p not in wave2 and p.family_id in {w.family_id for w in wave1}][:6]
    review = [p.id for p in ranked if p.scores.get("platform_risk") == "high" or
              any("moderation" in f for f in p.flags)]
    return {"wave1": [p.id for p in wave1], "wave2": [p.id for p in wave2], "refresh_alternates": refresh,
            "human_review": review}
