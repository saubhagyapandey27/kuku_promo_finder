"""Apply checked source intervals to promo records."""

from __future__ import annotations

import json

from typing import Dict, List, Optional, Tuple

from . import prompts

from .cuts import CutResult, Cutter, PromoSpec

from .mine import norm_id, structure_of

from .render import render_local

from .schemas import Promo, RepairOutput, Show

from .signals import cliff_signal, opening_arousal

BENIGN_SOFT = {"merged_adjacent_segments", "mid_shot_start", "outside_25_50s"}

def spec_for(p: Promo, check_quotes: bool = True) -> PromoSpec:
    return PromoSpec(segments=list(p.pick), hook_block=p.hook_block, cliff_block=p.cliff_block,
                     hook_quote=p.hook_quote, cliff_quote=p.cliff_quote, cliff_type=p.cliff_type,
                     flash_forward=p.flash_forward, check_quotes=check_quotes,
                     start_at_hook=p.editorial.get('start_at_hook',False),
                     stop_before_block=p.editorial.get('endpoint_before_block',''))

def apply_cut(p: Promo, res: CutResult, show: Show, cutter: Cutter) -> None:
    current = [s.as_dict() for s in res.segments]
    if p.segments != current:
        from .boundary import invalidate
        invalidate(p)
    p.segments = current
    p.duration_s = res.duration
    p.hard, p.soft, p.dead_air_s, p.cut_info = res.hard, res.soft, round(res.dead_air, 2), res.info
    if res.ok:
        p.status = "cut_ok"
        p.structure = structure_of(p, show)
        ix = cutter.ix[p.segments[0]["ep"]]
        cb = cutter.blocks[p.cliff_block]
        p.signals["cliff_signal"] = cliff_signal(cb, show.episode(cb.ep), p.cliff_type)
        p.signals["opening_arousal"] = round(opening_arousal(ix, p.segments[0]["start_s"]), 3)
    else:
        p.status = "candidate"
