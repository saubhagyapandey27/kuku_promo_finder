"""Preflight: before spending real calls, check every model a profile can use.

For each model: exists on the provider's live list; live price satisfies the price rule; a REAL call in the exact
shape the pipeline uses (dummy Pydantic schema + reasoning effort + temperature + provider object) succeeds, which
records a capability profile (structured-output mode, supports_temperature, moderated, data policy) the router uses.
Fails fast with a readable message.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from pydantic import BaseModel

from ..config import Config, ModelSpec
from ..util import write_json
from .router import Capability, Router


class Ping(BaseModel):
    ok: bool
    n: int
    word: str


@dataclass
class ModelCheck:
    alias: str
    id: str
    provider: str
    exists: Optional[bool] = None
    live_price_out: Optional[float] = None
    price_ok: bool = True
    call_ok: bool = False
    capability: Optional[dict] = None
    cost_usd: float = 0.0
    latency_s: float = 0.0
    notes: List[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.price_ok and self.call_ok and self.exists is not False


@dataclass
class PreflightReport:
    profile: str
    checks: List[ModelCheck] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)

    @property
    def available(self) -> List[str]:
        return [c.alias for c in self.checks if c.ok]

    def as_dict(self) -> dict:
        return {"profile": self.profile, "available": self.available, "problems": self.problems,
                "checks": [c.__dict__ for c in self.checks]}

    def format(self) -> str:
        rows = [f"Preflight for profile '{self.profile}':"]
        for c in self.checks:
            cap = c.capability or {}
            rows.append(f"  {'OK ' if c.ok else 'FAIL'} {c.alias:<22} {c.id:<42} "
                        f"mode={cap.get('mode', '-'):<11} temp={cap.get('supports_temperature', '-')!s:<5} "
                        f"moderated={cap.get('moderated', '-')!s:<5} {c.latency_s:4.1f}s ${c.cost_usd:.5f}"
                        + (f"  <- {c.error[:140]}" if c.error else "") + ("  " + "; ".join(c.notes) if c.notes else ""))
        for p in self.problems:
            rows.append("  ! " + p)
        return "\n".join(rows)


def fetch_openrouter_models(timeout: float = 20.0) -> Optional[Dict[str, dict]]:
    try:
        with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=timeout) as r:
            data = json.load(r)
        return {m["id"]: m for m in data.get("data", [])}
    except Exception:  # noqa: BLE001
        return None


def _models_for(cfg: Config, roles: Optional[List[str]] = None) -> List[ModelSpec]:
    seen: Dict[str, ModelSpec] = {}
    for role in (roles or cfg.active_roles()):
        for s in cfg.role_prefs(role):
            seen.setdefault(s.alias, s)
    return list(seen.values())


def check_live_listing(cfg: Config, spec: ModelSpec, live: Optional[Dict[str, dict]], chk: ModelCheck) -> None:
    if spec.provider != "openrouter" or live is None:
        if spec.provider == "openrouter" and live is None:
            chk.notes.append("OpenRouter model list unreachable; existence/price not verified")
            chk.price_ok = False
            chk.error = 'live price cannot be verified; no paid probe admitted'
        return
    m = live.get(spec.id)
    if m is None:
        chk.exists = False
        chk.error = f"model id '{spec.id}' not on OpenRouter's live list"
        return
    chk.exists = True
    try:
        chk.live_price_out = float(m["pricing"]["completion"]) * 1e6
    except Exception:  # noqa: BLE001
        chk.notes.append("live price unavailable")
        chk.price_ok = False
        chk.error = 'live output price unavailable'
        return
    cap = min(0.50, float(cfg.price_rule.get("max_out_per_m", 0.5)))
    if chk.live_price_out > cap + 1e-6:
        chk.price_ok = False
        chk.error = (f"live output price ${chk.live_price_out:.3f}/M exceeds the ${cap}/M rule "
                     f"(registry says ${spec.price_out})")
    elif abs(chk.live_price_out - spec.price_out) > max(0.02, 0.15 * spec.price_out):
        chk.notes.append(f"live price ${chk.live_price_out:.3f}/M differs from registry ${spec.price_out}/M")
    tp = m.get("top_provider") or {}
    if "is_moderated" in tp:
        chk.notes.append("moderated" if tp.get("is_moderated") else "unmoderated")


def _with_deadline(fn, seconds: float):
    """Run fn() and give up after `seconds` (a model that hangs, e.g. a 504 after minutes, must not stall every run).
    The worker is a daemon thread, so an abandoned call never blocks interpreter exit."""
    box: Dict[str, object] = {}

    def work():
        try:
            box["v"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["e"] = e
    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(seconds)
    if th.is_alive():
        raise TimeoutError(f"no answer within {seconds:.0f}s (model overloaded or unavailable)")
    if "e" in box:
        raise box["e"]  # type: ignore[misc]
    return box["v"]


def probe_role(router: Router, role: str, schema, system: str, user: str, *,
               spec: Optional[ModelSpec] = None, max_tokens: Optional[int] = None, timeout_s: float = 60):
    """Fresh role/schema probe. Callers supply a synthetic representative task, never cached connectivity evidence."""
    return _with_deadline(lambda: router.complete_json(
        role, system, user, schema, spec=spec, max_tokens=max_tokens or router.cfg.max_tokens(role),
        stage='role_preflight', item_id=role, tries=1, salt='role-probe:' + uuid.uuid4().hex), timeout_s)


def run_preflight(cfg: Config, router: Router, roles: Optional[List[str]] = None, live_list: bool = True,
                  out_path: Optional[str] = None, log=print, probe_timeout_s: float = 60.0) -> PreflightReport:
    rep = PreflightReport(profile=cfg.profile.name)
    rep.problems.extend(cfg.final_contract_problems())
    if rep.problems:
        return rep
    live = fetch_openrouter_models() if live_list else None
    specs = _models_for(cfg, roles)
    caps: Dict[str, Capability] = {}
    for sp in specs:
        chk = ModelCheck(alias=sp.alias, id=sp.id, provider=sp.provider)
        rep.checks.append(chk)
        bad = cfg.price_violation(sp)
        if bad:
            chk.price_ok, chk.error = False, bad
            continue
        if live_list:
            check_live_listing(cfg, sp, live, chk)
        if chk.exists is False or not chk.price_ok:
            continue
        key=sp.api_key_env or ('GEMINI_API_KEY' if sp.provider=='gemini' else 'OPENROUTER_API_KEY')
        missing_key=not router.credential(key)
        if missing_key:
            chk.error = "API key missing"
            continue
        cap = Capability(sp.alias, supports_temperature=sp.supports_temperature, moderated=sp.moderated,
                         data_policy="may train on prompts" if sp.trains else "no training")
        router.caps[sp.alias] = cap
        before = router.total_cost
        t0 = time.time()
        try:
            res = _with_deadline(lambda: router.complete_json(
                "preflight", "You are a connectivity test. Reply with the JSON object only.",
                'Return {"ok": true, "n": 7, "word": "kuku"}.', Ping, spec=sp, temperature=0.0, max_tokens=2000,
                stage="preflight", item_id=sp.alias, tries=1, salt='live-probe:' + uuid.uuid4().hex), probe_timeout_s)
            chk.call_ok = bool(res.obj.ok)
        except Exception as e:  # noqa: BLE001
            chk.error = f"{type(e).__name__}: {str(e)[:300]}"
            cap.ok = False
            cap.error = chk.error
        chk.latency_s = time.time() - t0
        chk.cost_usd = router.total_cost - before
        chk.capability = router.caps[sp.alias].as_dict()
        caps[sp.alias] = router.caps[sp.alias]
        log(f"  preflight {sp.alias}: {'ok' if chk.ok else 'FAIL ' + chk.error[:100]}")
    # per-role sanity: every role must still have at least one usable model
    avail = rep.available
    gf = None
    try:
        if avail:
            gf = cfg.generator_family(avail)
    except Exception as e:  # noqa: BLE001
        rep.problems.append(f"no usable generator model: {e}")
    for role in (roles or cfg.active_roles()):
        try:
            chain = cfg.resolve(role, gf if role in ("hook", "cold", "producer", "jury") else None, avail)
        except Exception as e:  # noqa: BLE001
            rep.problems.append(f"role '{role}': {e}")
            continue
        if not chain:
            rep.problems.append(f"role '{role}' has no usable model (check keys / family rule)")
    router.set_capabilities(caps)
    rep.problems.extend(cfg.final_contract_problems(avail))
    if out_path:
        write_json(out_path, rep.as_dict())
    return rep
