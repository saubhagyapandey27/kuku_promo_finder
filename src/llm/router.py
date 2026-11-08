"""LLM router: role -> model chain, disk cache, cost ledger, rate limits, structured-output modes, fallbacks.

Fallback policy (plan §8):
  * generator roles (bible/miner/composer/repair/naive): per call. A moderation block, refusal, outage or capability
    failure moves to the next model in the role's chain, recorded in the call's flags.
  * judge roles (hook/cold/producer/jury): PINNED per stage. A stage resolves one model; persistent 429/outage raises
    PersistentRateLimit so `run_stage` re-runs the WHOLE stage on the next model (never mixing judges inside a stage).
    The one documented exception: a moderation block on a single cold-viewer item is substituted and flagged.
"""
from __future__ import annotations

import json
import os
import threading
import time
import sqlite3
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..config import Config, ModelSpec
from ..util import (ModerationBlock, PersistentRateLimit, append_jsonl, parse_llm_json, read_json, retry_with_backoff,
                    sha, write_json)
from .base import (MODES, Backend, BudgetExceeded, CapabilityError, RawResult, TruncatedError, inline_schema)
from .budget import BudgetLedger


class SchemaError(ValueError):
    """The model kept returning output that doesn't validate against the schema."""


class StageFailed(RuntimeError):
    pass


@dataclass
class Capability:
    alias: str
    ok: bool = True
    mode: str = "json_schema"
    supports_temperature: bool = True
    moderated: Optional[bool] = None
    data_policy: str = ""
    error: str = ""

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class CallResult:
    obj: Any
    spec: ModelSpec
    flags: List[str] = field(default_factory=list)
    cached: bool = False
    cost: float = 0.0
    attempts: int = 1


class RateLimiter:
    """Minimum spacing between calls to one provider (RPM), thread-safe."""

    def __init__(self, rpm: int):
        self.interval = 60.0 / max(1, rpm)
        self._next = 0.0
        self._lock = threading.Lock()

    def acquire(self, sleep: Callable[[float], None] = time.sleep) -> None:
        with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next - now)
            self._next = max(now, self._next) + self.interval
        if wait > 0:
            sleep(wait)


class UsageCounter:
    """Persisted per-day call counter per provider (Gemini free-tier RPD planning)."""

    def __init__(self, path: Optional[str]):
        self.path = path
        self._lock = threading.Lock()
        self._memory: Dict[str, int] = {}

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d")

    def used(self, provider: str) -> int:
        return self._update(provider, 0, None)

    def bump(self, provider: str, n: int = 1) -> None:
        self._update(provider, n, None)

    def claim(self, provider: str, limit: Optional[int]) -> None:
        self._update(provider, 1, limit)

    def _update(self, provider: str, n: int, limit: Optional[int]) -> int:
        with self._lock:
            day = self._today()
            if not self.path:
                old = self._memory.get(day + provider, 0)
                if limit is not None and old + n > limit:
                    raise PersistentRateLimit('daily quota exhausted; no request sent')
                self._memory[day + provider] = old + n
                return old + n
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(self.path + '.sqlite3', timeout=15)
            try:
                db.execute('CREATE TABLE IF NOT EXISTS usage (day TEXT, model TEXT, n INTEGER, PRIMARY KEY(day,model))')
                db.execute('BEGIN IMMEDIATE')
                # Import the historical JSON counter without decreasing already-admitted usage.
                legacy = (read_json(self.path, {}) or {}).get(day, {}).get(provider, 0)
                row = db.execute('SELECT n FROM usage WHERE day=? AND model=?', (day, provider)).fetchone()
                old = max(int(legacy), row[0] if row else 0)
                if limit is not None and old + n > limit:
                    raise PersistentRateLimit('daily quota exhausted; no request sent')
                db.execute('INSERT OR REPLACE INTO usage VALUES (?,?,?)', (day, provider, old + n))
                db.commit()
                return old + n
            finally:
                db.close()


class Router:
    def __init__(self, cfg: Config, run_dir: Optional[str] = None, cache_dir: Optional[str] = None,
                 backends: Optional[Dict[str, Backend]] = None, budget_usd: Optional[float] = None,
                 available: Optional[Sequence[str]] = None, sleep: Callable[[float], None] = time.sleep,
                 schema_retries: int = 1, log: Optional[Callable[[str], None]] = None,
                 usage_path: Optional[str] = None, experiment_ledger: Optional[str] = None,
                 experiment_budget: Optional[float] = None, max_requests: int = 500,
                 deadline_s: float = 1800, credentials=None):
        self.cfg = cfg
        self.credentials = credentials
        self.run_dir = run_dir
        self.cache_dir = cache_dir
        self.sleep = sleep
        self.schema_retries = schema_retries
        self._log = log or (lambda m: print(m, flush=True))
        self.available = list(available) if available is not None else None
        self.caps: Dict[str, Capability] = {}
        self.budget = budget_usd
        if bool(experiment_ledger) != (experiment_budget is not None):
            raise ValueError('experiment ledger and experiment budget must be provided together')
        account = 'run:' + os.path.abspath(run_dir) if run_dir else 'invocation:' + str(time.time_ns())
        limits = {account: budget_usd}
        if experiment_ledger:
            limits['experiment'] = experiment_budget
        db_path = experiment_ledger or (os.path.join(run_dir, 'budget.sqlite3') if run_dir else None)
        old_ledger = Path(run_dir) / 'llm_calls.jsonl' if run_dir else None
        historical = [json.loads(line) for line in old_ledger.read_text(encoding='utf-8').splitlines() if line.strip()] \
            if old_ledger and old_ledger.exists() else []
        self.admission = BudgetLedger(db_path, limits, max_requests=max_requests, deadline_s=deadline_s,
                                     initial_costs={account: [float(c.get('cost', 0)) for c in historical if not c.get('cached')]})
        self._lock = threading.Lock()
        self.total_cost = 0.0
        self.cost_by: Dict[str, float] = defaultdict(float)
        self.calls_by: Dict[str, int] = defaultdict(int)
        for row in historical:
            if not row.get('cached'):
                cost=float(row.get('cost') or 0)
                self.total_cost+=cost
                key=row.get('role','')+':'+row.get('model','')
                self.cost_by[key]+=cost
                self.calls_by[key]+=1
        self.cache_hits = 0
        self.stage_models: Dict[str, str] = {}
        self.ledger_path = os.path.join(run_dir, "llm_calls.jsonl") if run_dir else None
        self.mod_path = os.path.join(run_dir, "moderation_hits.jsonl") if run_dir else None
        self.usage = UsageCounter(usage_path or (os.path.join(cache_dir, "usage.json") if cache_dir else None))
        self._limiters: Dict[str, RateLimiter] = {}
        self.backends: Dict[str, Backend] = backends if backends is not None else self._default_backends()
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

    # ------------------------------------------------------------------ setup helpers
    def credential(self, name):
        if self.credentials is not None:
            return self.credentials.get(name)
        return os.getenv(name) or (os.getenv('GOOGLE_API_KEY') if name == 'GEMINI_API_KEY' else None)

    def _default_backends(self) -> Dict[str, Backend]:
        from .gemini import GeminiBackend
        from .openai_compat import OpenAICompatBackend
        oc = OpenAICompatBackend(self.credentials)
        gemini = GeminiBackend(self.credentials)
        if self.credentials is not None:
            oc, gemini = self.credentials.wrap(oc), self.credentials.wrap(gemini)
        return {"openrouter": oc, "openai_compat": oc, "gemini": gemini}

    def log(self, msg: str) -> None:
        self._log(msg)

    def set_capabilities(self, caps: Dict[str, Capability]) -> None:
        self.caps.update(caps)
        self.available = [a for a, c in self.caps.items() if c.ok] if self.caps else self.available

    @property
    def generator_family(self) -> str:
        return self.cfg.generator_family(self.available)

    def chain(self, role: str) -> List[ModelSpec]:
        gf = None if role not in ("hook", "cold", "producer", "jury", "critic") else self.generator_family
        return self.cfg.resolve(role, gf, self.available)

    def jury_seats(self) -> List[ModelSpec]:
        return self.cfg.jury_seats(self.generator_family, self.available)

    def critic_seats(self) -> List[ModelSpec]:
        return self.cfg.critic_seats(self.generator_family, self.available)

    def _limiter(self, spec) -> RateLimiter:
        """One limiter per provider, or per model when the model has its own free-tier RPM (Gemini)."""
        key, rpm = (spec.alias, spec.free_rpm) if spec.free_rpm else (spec.provider, self.cfg.rpm(spec.provider))
        if key not in self._limiters:
            self._limiters[key] = RateLimiter(rpm)
        return self._limiters[key]

    @staticmethod
    def usage_key(spec) -> str:
        """Daily-quota counter key: per model for Gemini free tier, per provider otherwise."""
        return spec.alias if spec.provider == "gemini" else spec.provider

    # ------------------------------------------------------------------ cache + ledger
    def _cache_path(self, key: str) -> Optional[str]:
        return os.path.join(self.cache_dir, key + ".json") if self.cache_dir else None

    def _cache_get(self, key: str) -> Optional[dict]:
        p = self._cache_path(key)
        return read_json(p) if p and os.path.exists(p) else None

    def _cache_put(self, key: str, raw: RawResult) -> None:
        p = self._cache_path(key)
        if p:
            write_json(p, {"text": raw.text, "tokens_in": raw.tokens_in, "tokens_out": raw.tokens_out,
                           "cost": raw.cost, "mode": raw.mode})

    def _price(self, spec: ModelSpec, raw: RawResult) -> float:
        if raw.cost is not None:
            return float(raw.cost)
        return (raw.tokens_in * spec.price_in + raw.tokens_out * spec.price_out) / 1e6

    def _ledger(self, rec: dict) -> None:
        if self.ledger_path:
            append_jsonl(self.ledger_path, [rec])

    def _check_budget(self) -> None:
        self.admission.check_deadline()

    # ------------------------------------------------------------------ public API
    def complete_json(self, role: str, system: str, user: str, schema: Any, *, spec: Optional[ModelSpec] = None,
                      chain: Optional[Sequence[ModelSpec]] = None, pin: Optional[bool] = None, stage: str = "",
                      item_id: str = "", temperature: Optional[float] = None, max_tokens: Optional[int] = None,
                      on_moderation: str = "fallback", salt: str = "", tries: Optional[int] = None,
                      attempt_limit: Optional[int] = None, schema_retries: Optional[int] = None, validator=None) -> CallResult:
        """One structured LLM call with the role's fallback policy.

        spec     : pinned model (judge stages). chain: explicit candidate list (e.g. [pinned, substitute]).
        pin      : True -> persistent rate-limit raises PersistentRateLimit (stage-level failover) instead of moving on.
        on_moderation: 'fallback' (next model in the chain, flagged) | 'raise'.
        tries    : transport retries on transient errors (default 5; preflight probes use 1).
        """
        if chain is None:
            chain = [spec] if spec is not None else self.chain(role)
        if not chain:
            raise StageFailed(f"role '{role}' has no usable model (check preflight / family rules)")
        if pin is None:
            pin = spec is not None
        mt = max_tokens or self.cfg.max_tokens(role)
        flags: List[str] = []
        last: Optional[Exception] = None
        remaining = [attempt_limit] if attempt_limit is not None else None
        for i, sp in enumerate(chain):
            try:
                res = self._call_model(role, sp, system, user, schema, stage, item_id, temperature, mt, salt, tries,
                                       remaining, schema_retries, validator)
                if i > 0:
                    flags.append(f"fallback_from:{chain[0].alias}")
                res.flags = flags + res.flags
                return res
            except ModerationBlock as e:
                last = e
                flags.append(f"moderation:{sp.alias}")
                if self.mod_path:
                    append_jsonl(self.mod_path, [{"ts": time.time(), "role": role, "stage": stage, "item": item_id,
                                                  "model": sp.alias, "msg": str(e)[:400]}])
                self.log(f"  [{role}] moderation block on {sp.alias} ({item_id or stage})")
                if on_moderation == "raise":
                    raise
            except PersistentRateLimit as e:
                last = e
                if pin:
                    raise
                self.log(f"  [{role}] {sp.alias} unavailable ({str(e)[:100]}); trying next model")
                flags.append(f"unavailable:{sp.alias}")
            except (CapabilityError, SchemaError, TruncatedError) as e:
                last = e
                if remaining is not None and remaining[0] <= 0:
                    raise
                if pin and i == len(chain) - 1:
                    raise
                self.log(f"  [{role}] {sp.alias} failed ({type(e).__name__}: {str(e)[:120]}); trying next model")
                flags.append(f"failed:{sp.alias}")
        raise StageFailed(f"role '{role}': every model failed; last error: {last}")

    def review(self, role, system, user, schema, *, spec=None, validator=None, **kwargs):
        """At most two physical calls: one transient retry OR one schema/evidence correction.
        A valid editorial rejection is returned normally and is never resampled.
        Explicit role-chain fallback is operational only and recorded in CallResult.flags.
        """
        return self.complete_json(role,system,user,schema,spec=spec,validator=validator,
            tries=2,attempt_limit=2,schema_retries=1,**kwargs)

    def run_stage(self, stage: str, chain: Sequence[ModelSpec], fn: Callable[[ModelSpec], Any]) -> Any:
        """Run a whole judge stage pinned to one model; persistent rate limits re-run it on the next model."""
        last: Optional[Exception] = None
        for sp in chain:
            try:
                out = fn(sp)
                self.stage_models[stage] = sp.label
                return out
            except PersistentRateLimit as e:
                last = e
                self.log(f"  [{stage}] {sp.alias} persistently unavailable ({str(e)[:100]}); re-running the stage "
                         f"on the next model")
        raise StageFailed(f"stage {stage}: no model completed it; last error: {last}")

    def map(self, fn: Callable[[Any], Any], items: Sequence[Any], workers: int = 4) -> List[Any]:
        """Parallel map preserving order; exceptions are returned in place (callers decide how to handle them)."""
        def safe(x):
            try:
                return fn(x)
            except BudgetExceeded:
                raise
            except Exception as e:  # noqa: BLE001
                return e
        if workers <= 1 or len(items) <= 1:
            return [safe(x) for x in items]
        with ThreadPoolExecutor(max_workers=workers) as ex:
            return list(ex.map(safe, items))

    # ------------------------------------------------------------------ one model
    def _call_model(self, role: str, spec: ModelSpec, system: str, user: str, schema: Any, stage: str, item_id: str,
                    temperature: Optional[float], max_tokens: int, salt: str = "",
                    tries: Optional[int] = None, remaining: Optional[list] = None,
                    schema_retries: Optional[int] = None, validator=None) -> CallResult:
        backend = self.backends.get(spec.provider)
        if backend is None:
            raise CapabilityError(f"no backend for provider '{spec.provider}'")
        bad = self.cfg.price_violation(spec)
        if bad:
            raise ValueError(bad)
        cap = self.caps.get(spec.alias) or Capability(spec.alias, supports_temperature=spec.supports_temperature)
        temp = temperature if (cap.supports_temperature and spec.supports_temperature) else None
        dc = self._data_collection(spec)
        key = sha(spec.id, spec.effort, system, user, schema.__name__,
                  json.dumps(inline_schema(schema), sort_keys=True), temp, max_tokens, dc, salt, n=24)
        hit = self._cache_get(key)
        if hit:
            try:
                obj = parse_llm_json(hit["text"], schema)
                if validator:
                    validator(obj)
                with self._lock:
                    self.cache_hits += 1
                self._ledger({"ts": time.time(), "role": role, "stage": stage, "item": item_id, "model": spec.alias,
                              "model_id": spec.id, "effort": spec.effort, "cached": True, "cost": 0.0,
                              "prompt_hash": key})
                return CallResult(obj, spec, [], cached=True, cost=0.0)
            except ValueError:
                pass
        attempts = 0
        cur_user = user
        cur_max = max_tokens
        last_err: Optional[Exception] = None
        for _ in range((self.schema_retries if schema_retries is None else schema_retries) + 1):
            while True:                                           # transport loop (mode downgrade / truncation)
                self._check_budget()
                try:
                    def admitted_call():
                        nonlocal attempts
                        if remaining is not None:
                            if remaining[0] <= 0:
                                raise StageFailed('physical request allowance exhausted for this assessment')
                        self._limiter(spec).acquire(self.sleep)
                        # Byte count + schema and framing reserve conservatively bounds text tokens.
                        payload = json.dumps([system, cur_user, inline_schema(schema), inline_schema(schema)], ensure_ascii=True)
                        upper = ((len(payload.encode('utf-8')) + 4096) * spec.price_in + cur_max * spec.price_out) / 1e6
                        rid = self.admission.reserve(upper)
                        try:
                            self.usage.claim(self.usage_key(spec), spec.free_rpd)
                        except PersistentRateLimit:
                            self.admission.settle(rid, 0)
                            raise
                        if remaining is not None:
                            remaining[0] -= 1
                        attempts += 1
                        try:
                            raw = backend.call(spec, system, cur_user, schema, max_tokens=cur_max, temperature=temp,
                                               mode=cap.mode, data_collection=dc)
                        except BaseException as exc:
                            raw_error = getattr(exc, 'raw', None)
                            if raw_error is not None:
                                self._record_usage(role, spec, raw_error, stage, item_id, key, attempts, temp, dc)
                            elif self.run_dir:
                                append_jsonl(os.path.join(self.run_dir,'request_errors.jsonl'),[{
                                    'role':role,'stage':stage,'item':item_id,'model':spec.label,
                                    'reservation_id':rid,'attempt':attempts,'error':type(exc).__name__+': '+str(exc)[:400],
                                    'billing':'unknown; reservation retained unless capability failure'}])
                            known = self._price(spec, raw_error) if raw_error is not None else (0 if isinstance(exc, CapabilityError) else None)
                            self.admission.settle(rid, known)
                            raise
                        self._record_usage(role, spec, raw, stage, item_id, key, attempts, temp, dc)
                        known = self._price(spec, raw) if raw.cost is not None or raw.tokens_in or raw.tokens_out else None
                        self.admission.settle(rid, known)
                        return raw
                    raw = retry_with_backoff(
                        admitted_call,
                        label=f"{role}/{spec.alias}", sleep=self.sleep, on_wait=self.log, tries=tries or 5)
                except CapabilityError as e:
                    if temp is not None and "temperature" in str(e).lower():
                        self.log(f"  [{spec.alias}] temperature not accepted; dropping it for this model")
                        cap.supports_temperature = False
                        self.caps[spec.alias] = cap
                        temp = None
                        continue
                    nxt = self._next_mode(cap.mode)
                    if nxt is None or spec.provider == "gemini":
                        cap.ok = False
                        cap.error = str(e)[:300]
                        self.caps[spec.alias] = cap
                        raise
                    self.log(f"  [{spec.alias}] structured-output mode {cap.mode} not accepted ({str(e)[:100]}); "
                             f"falling back to {nxt}")
                    cap.mode = nxt
                    self.caps[spec.alias] = cap
                    continue
                except TruncatedError as e:
                    if remaining is not None and remaining[0] <= 0:
                        raise
                    if cur_max >= 64000:
                        raise
                    cur_max = min(64000, int(cur_max * 1.6))
                    self.log(f"  [{spec.alias}] output truncated ({str(e)[:80]}); retrying with max_tokens={cur_max}")
                    continue
                break
            cost = self._price(spec, raw)
            try:
                obj = parse_llm_json(raw.text, schema)
                if validator:
                    validator(obj)
            except ValueError as e:
                last_err = e
                self.log(f"  [{role}/{spec.alias}] invalid output ({str(e)[:140]})")
                cur_user = (user + "\n\n---\nYour previous reply could not be used: " + str(e)[:300] +
                            "\nReply again with ONLY the corrected JSON object.")
                continue
            self._cache_put(key, raw)
            return CallResult(obj, spec, [], cached=False, cost=cost, attempts=attempts)
        raise SchemaError(f"{spec.alias}: no valid {schema.__name__} after {(self.schema_retries if schema_retries is None else schema_retries) + 1} tries: {last_err}")

    def _record_usage(self, role, spec, raw, stage, item_id, key, attempts, temp, dc):
        cost = self._price(spec, raw)
        if self.run_dir:
            write_json(os.path.join(self.run_dir,'responses',sha(key,item_id,attempts,time.time_ns(),n=32)+'.json'),
                {'role':role,'item':item_id,'model':spec.label,'prompt_hash':key,'text':raw.text,
                 'tokens_in':raw.tokens_in,'tokens_out':raw.tokens_out,'cost_usd':cost,'finish_reason':raw.finish_reason})
        with self._lock:
            self.total_cost += cost
            self.cost_by[f'{role}:{spec.alias}'] += cost
            self.calls_by[f'{role}:{spec.alias}'] += 1
        self._ledger({'ts': time.time(), 'role': role, 'stage': stage, 'item': item_id, 'model': spec.alias,
                      'model_id': spec.id, 'effort': spec.effort, 'provider_used': raw.provider_used,
                      'mode': raw.mode, 'prompt_hash': key, 'tokens_in': raw.tokens_in,
                      'tokens_out': raw.tokens_out, 'tokens_reasoning': raw.tokens_reasoning,
                      'cost': round(cost, 6), 'latency_s': round(raw.latency_s, 2), 'attempt': attempts,
                      'temperature': temp, 'cached': False, 'data_collection': dc, 'finish_reason': raw.finish_reason})

    @staticmethod
    def _next_mode(mode: str) -> Optional[str]:
        i = MODES.index(mode) if mode in MODES else len(MODES) - 1
        return MODES[i + 1] if i + 1 < len(MODES) else None

    def _data_collection(self, spec: ModelSpec) -> Optional[str]:
        """'allow' only for training-tier models (Muse contributor / free tiers); 'deny' for everything else, and
        everywhere when the profile is `private`."""
        if spec.provider != "openrouter":
            return None
        if self.cfg.profile.data_collection == "deny":
            return "deny"
        return "allow" if spec.trains else "deny"

    # ------------------------------------------------------------------ reporting
    def cost_summary(self) -> dict:
        return {"total_usd": round(self.total_cost, 4), "by_role_model": {k: round(v, 4) for k, v in self.cost_by.items()},
                "calls": dict(self.calls_by), "cache_hits": self.cache_hits, "stage_models": dict(self.stage_models),
                "budget_usd": self.budget, "admission": self.admission.summary()}
