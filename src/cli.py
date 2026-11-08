"""Command-line entry points for parsing, preflight and the current pipeline."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import List, Optional

from .config import ROOT, Config, load_env
from .llm.base import BudgetExceeded
from .util import parse_ranges, write_json


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "show"


def load_show(input_path: str, title: Optional[str] = None, fmt_path: Optional[str] = None):
    from .load import load_texts
    from .parse import load_format, parse_show
    texts = load_texts(input_path)
    fmt = load_format(fmt_path) if fmt_path else load_format()
    t = title or Path(input_path).stem.replace("_", " ")
    show, report = parse_show(texts, fmt, t)
    return show, report


def cmd_parse(a) -> int:
    show, rep = load_show(a.input, a.title, a.format)
    print(f"parsed {rep.episodes} episode(s), {rep.blocks} blocks, {rep.lines} dialogue lines")
    for pe in rep.per_episode[: (None if a.verbose else 5)]:
        print("  ", pe)
    if not a.verbose and len(rep.per_episode) > 5:
        print(f"   ... ({len(rep.per_episode) - 5} more; --verbose to list all)")
    for i in rep.issues[:40]:
        print(f"  [{i.level}] ep={i.ep} line={i.line}: {i.msg}")
    if len(rep.issues) > 40:
        print(f"  ... {len(rep.issues) - 40} more issue(s)")
    if a.json:
        write_json(a.json, {"report": rep.model_dump()})
        print("report written to", a.json)
    return 1 if rep.errors else 0


def make_router(cfg: Config, run_dir: Optional[str], budget: Optional[float], log=print, options=None, cache_root=None, credentials=None):
    from .llm.router import Router
    base = Path(cache_root) if cache_root else ROOT / '.cache'
    cache = str(base / 'llm')
    return Router(cfg, run_dir=run_dir, cache_dir=cache, budget_usd=budget, log=log, credentials=credentials,
                  usage_path=str(base / 'usage.json'),
                  experiment_ledger=getattr(options, 'experiment_ledger', None),
                  experiment_budget=getattr(options, 'experiment_budget', None),
                  max_requests=getattr(options, 'max_requests', 500),
                  deadline_s=getattr(options, 'deadline_seconds', 1800))


def cmd_preflight(a) -> int:
    from .llm.preflight import run_preflight
    load_env()
    cfg = Config.load(a.profile)
    router = make_router(cfg, None, a.budget, options=a)
    out = a.out or str(ROOT / ".cache" / f"preflight_{cfg.profile.name}.json")
    rep = run_preflight(cfg, router, out_path=out, log=print)
    print(rep.format())
    print(f"(saved {out}; probe spend ${router.total_cost:.4f})")
    return 0 if (rep.available and not rep.problems) else 1


def cmd_run(a) -> int:
    from .llm.preflight import run_preflight
    from .runner import Pipeline, subset_show
    load_env()
    cfg = Config.load(a.profile)
    show, rep = load_show(a.input, a.title, a.format)
    if rep.errors:
        print("parse errors; fix the input or configs/format.yaml first (python -m src parse --input ...)")
        for i in rep.errors[:10]:
            print(f"  ep={i.ep} line={i.line}: {i.msg}")
        return 1
    eps = parse_ranges(a.episodes)
    show = subset_show(show, eps)
    rid = a.run_id or f"{time.strftime('%Y%m%d-%H%M%S')}_{cfg.profile.name}"
    run_dir = str(Path(a.out) / _slug(show.title) / rid)
    manifest_path=Path(run_dir)/'quality_dependencies.json'
    if manifest_path.exists() and json.loads(manifest_path.read_text(encoding='utf-8')).get('mode')=='finalize_only':
        raise ValueError('this is a frozen finalization artifact; finalize into a new directory instead of running it')
    if cfg.profile.experiments.get('final_cuts', True) and (Path(run_dir)/'.done').exists() and not (Path(run_dir)/'quality_dependencies.json').exists():
        raise ValueError('legacy run is frozen; choose a NEW --run-id for final-cut processing')
    os.makedirs(run_dir, exist_ok=True)
    write_json(os.path.join(run_dir, "00_parse_report.json"), rep.model_dump())
    write_json(os.path.join(run_dir, "eps.json"), [e.ep for e in show.episodes])
    write_json(os.path.join(run_dir, "run_meta.json"), {"input": os.path.abspath(a.input), "profile": cfg.profile.name,
                                                         "title": show.title, "started": time.strftime("%Y-%m-%d %H:%M:%S")})
    router = make_router(cfg, run_dir, a.budget, options=a)
    if not a.no_preflight:
        pf = run_preflight(cfg, router, out_path=os.path.join(run_dir, "preflight.json"))
        print(pf.format())
        if not pf.available or pf.problems:
            print("preflight failed; fix the problems above (keys in .env, model availability) and retry")
            return 2
    pipe = Pipeline(show, cfg, router, run_dir, resume=not a.no_resume, force_stage=a.force_stage, workers=a.workers)
    recs = pipe.run(until=a.until)
    print(f"\n{len(recs)} promos -> {run_dir}/promos.json (+ .csv)")
    for r in recs[: a.show]:
        segs = " | ".join(f"E{s['episode']:02d} {s['start']}-{s['end']}" for s in r["segments"])
        print(f"  #{r['rank']:>2} {r['promo_id']:<8} {r['duration_s']:>5.1f}s {r['structure']:<12} {segs}  | {r['title'][:50]}")
    return 3 if getattr(pipe, 'quality_incomplete', False) else 0



def main(argv=None):
    ap = argparse.ArgumentParser(description='Find ranked 20–60 second promo timestamps.')
    sub = ap.add_subparsers(dest='command', required=True)
    p = sub.add_parser('parse', help='validate timestamped screenplay input without model calls')
    p.add_argument('--input', required=True)
    p.add_argument('--title')
    p.add_argument('--format')
    p.add_argument('--json')
    p.add_argument('--verbose', action='store_true')
    p.set_defaults(fn=cmd_parse)
    p = sub.add_parser('preflight', help='verify model availability and current prices')
    p.add_argument('--profile', default='quality')
    p.add_argument('--budget', type=float, required=True)
    p.add_argument('--out')
    p.set_defaults(fn=cmd_preflight)
    p = sub.add_parser('run', help='run the current blind-first promo selection pipeline')
    p.add_argument('--input', required=True)
    p.add_argument('--profile', default='quality', help='quality or a custom YAML path')
    p.add_argument('--episodes', help='optional subset, for example 1-5,8')
    p.add_argument('--until', default='S9_export', choices=['S1_bible','S3_mine','S9_export'])
    p.add_argument('--out', default='runs')
    p.add_argument('--run-id')
    p.add_argument('--title')
    p.add_argument('--format')
    p.add_argument('--budget', type=float, required=True)
    p.add_argument('--workers', type=int, default=4)
    p.add_argument('--force-stage')
    p.add_argument('--no-resume', action='store_true')
    p.add_argument('--no-preflight', action='store_true')
    p.add_argument('--show', type=int, default=10)
    p.set_defaults(fn=cmd_run)
    for command in ('preflight','run'):
        p = sub.choices[command]
        p.add_argument('--experiment-ledger')
        p.add_argument('--experiment-budget', type=float)
        p.add_argument('--max-requests', type=int, default=600)
        p.add_argument('--deadline-seconds', type=float, default=21600)
    args = ap.parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except BudgetExceeded as exc:
        print('incomplete: '+str(exc))
        return 3
    except KeyboardInterrupt:
        print('Interrupted. Completed work is checkpointed.')
        return 130


if __name__ == '__main__':
    sys.exit(main())
