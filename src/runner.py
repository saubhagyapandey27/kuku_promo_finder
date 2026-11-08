"""Current blind-first pipeline orchestration and proof-checked export."""
from __future__ import annotations

import json
import os
import time
from typing import Callable, Dict, List, Optional, Sequence

import yaml

from . import bible as bible_mod
from . import mine as mine_mod
from .config import Config
from .cuts import Cutter
from .export import promo_record, promos_csv
from .llm.router import Router
from .render import JudgeRenderer
from .schemas import Promo, Show
from .scrub import Scrubber, infer_entries
from .util import Checkpoint, append_jsonl, read_json, read_jsonl, write_json, sha
from pathlib import Path

STAGE_ORDER = ["S1_bible", "S3_mine", "S4_compose", "S5_cut", "S6_dedup", "S7a_hook", "S7_prefilter", "S7b_cold",
               "S7c_producer", "S8_rank", "S9_export"]


def subset_show(show: Show, eps: Optional[Sequence[int]]) -> Show:
    if eps is None:
        selected=show
    else:
        keep=set(eps)
        selected=Show(title=show.title,episodes=[e for e in show.episodes if e.ep in keep])
    if not selected.episodes:
        raise ValueError('episode subset is empty; no inference requested')
    return selected


def build_scrubber(show: Show, bible_d: dict) -> Scrubber:
    """Bible entries (visual tags per episode range, aliases, spoiler terms) + inferred fallback entries for any
    speaker label / name the bible missed, so nothing leaks."""
    sc = Scrubber.from_bible(bible_d)
    have = {s.lower() for e in sc.entries for s in e.surfaces}
    extra = [e for e in infer_entries(show) if not any(s.lower() in have for s in e.surfaces)]
    if not extra:
        return sc
    return Scrubber(sc.entries + extra, sc.spoiler_terms, sc.label_map, sc.alias_confidence)


class Pipeline:
    def __init__(self, show: Show, cfg: Config, router: Router, run_dir: str, log: Optional[Callable[[str], None]] = None,
                 resume: bool = True, force_stage: Optional[str] = None, workers: int = 4):
        subset_show(show,None)
        self.show, self.cfg, self.router, self.run_dir = show, cfg, router, run_dir
        problems=cfg.final_contract_problems(router.available)
        if problems:
            raise ValueError('unsupported final-cut profile: '+'; '.join(problems))
        os.makedirs(run_dir, exist_ok=True)
        self.final_mode = cfg.profile.experiments.get('final_cuts', True)
        if self.final_mode:
            root = Path(__file__).parent
            digest = sha(show.model_dump(), cfg.profile.__dict__, cfg._models,
                         [(str(p.relative_to(root)), p.read_bytes()) for p in sorted(root.rglob('*'))
                          if p.suffix in ('.py', '.md')], n=32)
            manifest = self.p('quality_dependencies.json')
            prior = read_json(manifest)
            if (prior and prior.get('fingerprint') != digest) or (not prior and Path(self.p('.done')).exists()
                                                                 and any(Path(self.p('.done')).iterdir())):
                raise ValueError('source/code/prompt/config changed or legacy run: preserve it and choose a new run ID')
            write_json(manifest, {'fingerprint': digest, 'mode': 'final_script_edl'})
        self.ck = Checkpoint(run_dir)
        self.resume = resume
        self.workers = workers
        self._print = log or (lambda m: print(m, flush=True))
        router._log = self.log
        if force_stage:
            self.ck.clear(force_stage)
        self.cutter = Cutter(show, {'boundary_clearance_s': 0.0} if self.final_mode else None)
        if not os.path.exists(self.p("00_show.json")):       # parsed screenplay: the Promo Board's read-along needs it
            write_json(self.p("00_show.json"), show.model_dump())
        self.bible_d: dict = {}
        self.bible_meta: dict = {}
        self.promos: List[Promo] = []                 # working set (changes by stage)
        self.pool: List[Promo] = []
        self.all_cut: List[Promo] = []
        self.drops: List[dict] = []
        self.jr: Optional[JudgeRenderer] = None
        self.families: Dict[str, List[str]] = {}
        self.stage_notes: Dict[str, dict] = {}
        self.t0 = time.time()

    # ------------------------------------------------------------------ plumbing
    def p(self, name: str) -> str:
        return os.path.join(self.run_dir, name)

    def log(self, msg: str) -> None:
        self._print(msg)
        try:
            append_jsonl(self.p("progress.jsonl"), [{"ts": time.time(), "msg": msg}])
        except Exception:  # noqa: BLE001
            pass

    def stage_start(self, stage: str) -> None:
        self.log(f"== {stage} ==")
        append_jsonl(self.p("progress.jsonl"), [{"ts": time.time(), "stage": stage, "event": "start",
                                                  "elapsed_s": round(time.time() - self.t0, 1)}])

    def _save(self, stage: str, promos: List[Promo]) -> None:
        os.makedirs(self.p("state"), exist_ok=True)
        with open(self.p(f"state/{stage}.json"), "w", encoding="utf-8") as f:
            json.dump([x.model_dump() for x in promos], f, ensure_ascii=False)

    def _load(self, stage: str) -> List[Promo]:
        d = read_json(self.p(f"state/{stage}.json"), []) or []
        return [Promo.model_validate(x) for x in d]

    def _done(self, stage: str) -> bool:
        return self.resume and self.ck.is_done(stage)

    def _finish(self, stage: str, promos: Optional[List[Promo]] = None, note: str = "") -> None:
        if promos is not None:
            self._save(stage, promos)
        self.ck.mark(stage, note)

    def renderer(self) -> JudgeRenderer:
        if self.jr is None:
            if self.final_mode:
                from .viewer import ViewerRenderer
                self.jr=ViewerRenderer(self.show,build_scrubber(self.show,self.bible_d))
            else:
                self.jr = JudgeRenderer(self.show, build_scrubber(self.show, self.bible_d), self.cutter.ix)
        return self.jr

    # ------------------------------------------------------------------ stages
    def s1_bible(self) -> None:
        if self._done("S1_bible"):
            d = read_json(self.p("01_bible.json"))
            self.bible_d, self.bible_meta = d["bible"], d.get("meta", {})
            self.log("S1 bible: loaded from checkpoint")
            return
        self.stage_start("S1_bible")
        _, self.bible_d, self.bible_meta = bible_mod.build_bible(self.show, self.router, self.log)
        write_json(self.p("01_bible.json"), {"bible": self.bible_d, "meta": self.bible_meta})
        self.log(f"S1 bible: {len(self.bible_d['characters'])} characters, {len(self.bible_d['acquisition_angles'])} "
                 f"angles, label resolution {self.bible_meta['label_resolution']:.0%}, flags {self.bible_meta['flags']}")
        self._finish("S1_bible")

    def s3_mine(self) -> None:
        if self._done("S3_mine"):
            self.promos = self._load("S3_mine")
            self.log(f"S3 mine: loaded {len(self.promos)} candidates from checkpoint")
            return
        self.stage_start("S3_mine")
        promos, drops = mine_mod.mine_all(self.show, self.bible_d, self.router, workers=min(self.workers, 6), log=self.log)
        self.drops += drops
        self.promos = promos
        self.mining_failures=[d for d in drops if d.get('reason','').startswith('window_failed')]
        write_json(self.p('mining_coverage.json'),{'expected_episodes':[e.ep for e in self.show.episodes],
            'failed_windows':self.mining_failures})
        self.log(f"S3 mine: {len(promos)} candidates ({len(drops)} dropped for bad block IDs / failed windows)")
        with open(self.p("03_candidates.jsonl"), "w", encoding="utf-8") as f:
            for x in promos:
                f.write(json.dumps(x.model_dump(), ensure_ascii=False) + "\n")
        self._finish("S3_mine", promos)

    # ------------------------------------------------------------------ export
    def export(self, ranked: Optional[List[Promo]] = None, note: str = "") -> List[dict]:
        if ranked is None:
            ranked = getattr(self, "ranked", None) or sorted(
                self.pool or self.promos, key=lambda p: -p.scores.get("composite", p.scores.get("pre", 0)))
        if self.final_mode:
            from .boundary import eligible
            from .readiness import valid_proof
            from .finalrank import valid_proof as valid_jury
            rejected = [p.id for p in ranked if p.status != 'final_script_edl' or not eligible(p, self.renderer(), self.cutter)[0]
                        or (self.cfg.profile.ranking.get('mode')!='rubric' and not valid_jury(p,self.renderer(),self.cfg))
                        or not valid_proof(p,self.renderer(),self.cfg)]
            if rejected:
                raise ValueError('final export refused stale or ineligible cuts: ' + ', '.join(rejected))
        by_family: Dict[str, List[str]] = {}
        for p in self.promos:
            by_family.setdefault(p.family_id, []).append(p.id)
        recs = []
        for i, p in enumerate(ranked, 1):
            alts = p.editorial.get("alternate_ids",[]) + [x for x in by_family.get(p.family_id, []) if x != p.id]
            recs.append(promo_record(p, i, self.show, alts))
        write_json(self.p("promos.json"), {"show": self.show.title, "n": len(recs), "note": note, "run_kind": "new_run", "ranking_method": self.cfg.profile.ranking, "posting_plan":getattr(self,"plan",{}), "coverage":read_json(self.p("quality_coverage.json"),{}), "cost":self.router.cost_summary(), "promos": recs})
        with open(self.p("promos.csv"), "w", encoding="utf-8-sig", newline="") as f:
            f.write(promos_csv(recs))
        write_json(self.p("cost_summary.json"), self.router.cost_summary() | {"elapsed_s": round(time.time() - self.t0, 1)})
        try:
            from .report import make_report
            with open(self.p("report.md"), "w", encoding="utf-8") as f:
                f.write(make_report(self, recs))
        except Exception as e:  # noqa: BLE001
            self.log(f"report.md skipped: {type(e).__name__}: {str(e)[:160]}")
        with open(self.p("config_used.yaml"), "w", encoding="utf-8") as f:
            yaml.safe_dump({"profile": self.cfg.profile.name,
                            "roles": {r: [f"{a}:{e}" for a, e in v] for r, v in self.cfg.profile.roles.items()},
                            "ranking": self.cfg.profile.ranking, "jury": self.cfg.profile.jury, "pool": self.cfg.profile.pool,
                            "experiments": self.cfg.profile.experiments,
                            "stage_models": self.router.stage_models}, f)
        return recs

    # ------------------------------------------------------------------ driver
    def run(self, until: str = "S9_export") -> List[dict]:
        from .llm.base import BudgetExceeded
        try:
            result = self._run(until)
        except BudgetExceeded as exc:
            write_json(self.p('execution_status.json'), {'state': 'incomplete', 'reason': str(exc),
                        'requested_stage': until, 'cost': self.router.cost_summary()})
            write_json(self.p('cost_summary.json'), self.router.cost_summary())
            raise
        write_json(self.p('execution_status.json'), {'state': 'incomplete_coverage' if getattr(self, 'quality_incomplete', False)
                   else 'completed_requested_stages', 'requested_stage': until})
        return result

    def _run(self, until: str = "S9_export") -> List[dict]:
        if self.final_mode:
            self.s1_bible()
            if until == 'S1_bible':
                return self.export([], note='No final cuts: source analysis only')
            self.s3_mine()
            if until in ('S3_mine', 'S4_compose'):
                return self.export([], note='No final cuts: candidates require semantic assessment')
            from .finalcuts import run_final_cuts
            return run_final_cuts(self)
