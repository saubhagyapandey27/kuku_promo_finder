"""UI-independent background pipeline jobs.

A job runs the same Pipeline as the CLI in a daemon thread and reports through files in its run dir:
    status.json    {state: running|done|error, started, finished, error}
    progress.jsonl stage events and log lines (the UI polls this)
so UI reruns, widget clicks and tab switches never kill or lose a run.
"""
from __future__ import annotations

import os
import re
import threading
import time
import traceback
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .config import Config
from .util import read_json, read_jsonl, write_json


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "show"


def job_status(run_dir: str) -> dict:
    return read_json(os.path.join(run_dir, "status.json"), {"state": "unknown"}) or {"state": "unknown"}


def read_progress(run_dir: str, n: int = 14) -> Tuple[List[dict], Optional[str]]:
    rows = read_jsonl(os.path.join(run_dir, "progress.jsonl"))
    stage = None
    for r in rows:
        if r.get("event") == "start":
            stage = r.get("stage")
    return rows[-n:], stage


def run_cost(run_dir: str) -> float:
    return round(sum(float(r.get("cost") or 0.0) for r in read_jsonl(os.path.join(run_dir, "llm_calls.jsonl"))), 4)


def feedback_scope(run_dir, records):
    from .util import sha
    return str(Path(run_dir).resolve())+':'+sha([(r['promo_id'],r['segments'],
        r.get('eligibility',{}).get('fingerprint')) for r in records],n=32)


def _worker(texts, title: str, profile: str, eps: Optional[Sequence[int]], budget: float, run_dir: str,
            preflight: bool, credentials=None) -> None:
    from .cli import make_router
    from .llm.preflight import run_preflight
    from .parse import parse_show
    from .runner import Pipeline, subset_show
    st = {"state": "running", "started": time.strftime("%Y-%m-%d %H:%M:%S"), "profile": profile}
    write_json(os.path.join(run_dir, "status.json"), st)
    try:
        cfg = Config.load(profile)
        show, rep = parse_show(texts, None, title)
        if rep.errors:
            raise ValueError("the input could not be parsed: " + "; ".join(i.msg for i in rep.errors[:3]))
        show = subset_show(show, eps)
        write_json(os.path.join(run_dir, "00_parse_report.json"), rep.model_dump())
        write_json(os.path.join(run_dir, "eps.json"), [e.ep for e in show.episodes])
        router = make_router(cfg, run_dir, budget, cache_root=Path(run_dir) / 'web-cache', credentials=credentials)
        if preflight:
            pf = run_preflight(cfg, router, out_path=os.path.join(run_dir, "preflight.json"), log=lambda m: None)
            if not pf.available or pf.problems:
                raise RuntimeError("preflight failed: " + " | ".join(pf.problems or [c.error for c in pf.checks if c.error])[:600])
        pipe = Pipeline(show, cfg, router, run_dir, log=lambda m: None)
        pipe.run()
        st.update(state='incomplete' if getattr(pipe, 'quality_incomplete', False) else 'done',
                  finished=time.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception as e:  # noqa: BLE001
        if credentials is not None:
            st.update(state='error', finished=time.strftime('%Y-%m-%d %H:%M:%S'),
                      error='Visitor-key analysis failed; check provider access, active routes and spend cap.')
        else:
            st.update(state="error", finished=time.strftime("%Y-%m-%d %H:%M:%S"), error=f"{type(e).__name__}: {str(e)[:600]}",
                      trace=traceback.format_exc()[-1500:])
    finally:
        if credentials is not None: credentials.clear()
    write_json(os.path.join(run_dir, "status.json"), st)


class JobRegistry:
    """Holds threads started in this process; not a durable queue."""

    def __init__(self):
        self.threads: Dict[str, threading.Thread] = {}

    def start(self, texts, title: str, profile: str, eps: Optional[Sequence[int]], budget: float, runs_root: str,
              preflight: bool = True, credentials=None) -> str:
        rid = f"{time.strftime('%Y%m%d-%H%M%S')}_{profile}"
        run_dir = str(Path(runs_root) / _slug(title) / rid)
        os.makedirs(run_dir, exist_ok=True)
        write_json(os.path.join(run_dir, "status.json"), {"state": "running", "started": time.strftime("%Y-%m-%d %H:%M:%S")})
        def execute():
            try:
                _worker(texts, title, profile, eps, budget, run_dir, preflight, credentials)
            finally:
                if credentials is not None: credentials.clear()
        t = threading.Thread(target=execute, daemon=True,
                             name=f"job-{rid}")
        self.threads[run_dir] = t
        t.start()
        return run_dir

    def alive(self, run_dir: str) -> bool:
        t = self.threads.get(run_dir)
        return bool(t and t.is_alive())


def list_runs(runs_root: str) -> List[str]:
    """Flat saved outputs or run dirs (newest first) with promos or a status file."""
    root = Path(runs_root)
    if not root.exists():
        return []
    dirs = [p.parent for p in root.glob("*/*/status.json")] + [p.parent for p in root.glob("*/*/promos.json")] + \
           [p.parent for p in root.glob("*/promos.json")] + [p.parent for p in root.glob("promos.json")]
    uniq = sorted({str(d) for d in dirs}, key=lambda d: os.path.getmtime(d), reverse=True)
    return uniq
