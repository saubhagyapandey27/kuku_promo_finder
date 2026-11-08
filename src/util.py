"""Small shared helpers (ported and generalised from the TRM podcast pipeline `pipeline/utils.py`)."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

from pydantic import ValidationError

_LOCK = threading.Lock()


def sha(*parts: Any, n: int = 16) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p if isinstance(p, bytes) else str(p).encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()[:n]


# ---------------------------------------------------------------------------------- retry
class PersistentRateLimit(RuntimeError):
    """Raised when a model stays rate-limited / unavailable after the backoff budget (stage-level failover)."""


class ModerationBlock(RuntimeError):
    """Provider refused the content (moderation / safety filter)."""

    def __init__(self, msg: str, detail: Any = None):
        super().__init__(msg)
        self.detail = detail


_TRANSIENT = ("429", "resource_exhausted", "503", "unavailable", "500", "internal server error", "504",
              "gateway timeout", "temporary", "demand", "rate_limit", "rate limit", "server_error", "busy",
              "overloaded", "timed out", "timeout", "connection", "502", "bad gateway", "empty response")


def is_transient_error(e: Exception) -> bool:
    if isinstance(e, (ModerationBlock, PersistentRateLimit)) or getattr(e, 'retryable', True) is False:
        return False
    s = str(e).lower()
    return any(t in s for t in _TRANSIENT)


def retry_with_backoff(fn: Callable[[], Any], label: str = "LLM", tries: int = 5, base: float = 2.0,
                       cap: float = 30.0, sleep: Callable[[float], None] = time.sleep,
                       on_wait: Optional[Callable[[str], None]] = None) -> Any:
    """Call fn() with exponential backoff on transient errors. Raises PersistentRateLimit when the budget is spent."""
    last: Optional[Exception] = None
    for attempt in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            if not is_transient_error(e):
                raise
            wait = min(cap, base * (2 ** attempt))
            if on_wait:
                on_wait(f"[{label}] transient ({str(e)[:120]}); retry in {wait:.0f}s ({attempt + 1}/{tries})")
            if attempt < tries - 1:
                sleep(wait)
    raise PersistentRateLimit(f"{label}: still failing after {tries} tries: {str(last)[:300]}") from last


# ---------------------------------------------------------------------------------- tolerant JSON
def fuzzy_normalize_schema(data: Any, schema_class: Any) -> Any:
    """Remap abbreviated / near-miss field names to the Pydantic field names (recursive)."""
    if not hasattr(schema_class, "model_fields"):
        return data
    if isinstance(data, list):
        return [fuzzy_normalize_schema(x, schema_class) for x in data]
    if not isinstance(data, dict):
        return data
    expected = set(schema_class.model_fields.keys())
    out: Dict[str, Any] = {}
    for key, value in data.items():
        if key in expected:
            tk = key
        else:
            m = difflib.get_close_matches(key, expected, n=1, cutoff=0.6)
            tk = m[0] if m else key
        fi = schema_class.model_fields.get(tk)
        if fi is not None:
            ann = fi.annotation
            origin = getattr(ann, "__origin__", None)
            args = getattr(ann, "__args__", ())
            if origin is list and args and hasattr(args[0], "model_fields") and isinstance(value, list):
                value = [fuzzy_normalize_schema(x, args[0]) for x in value]
            elif hasattr(ann, "model_fields") and isinstance(value, dict):
                value = fuzzy_normalize_schema(value, ann)
            else:
                # Optional[List[Model]] / Optional[Model]
                for a in args:
                    if hasattr(a, "model_fields") and isinstance(value, dict):
                        value = fuzzy_normalize_schema(value, a)
                    elif getattr(a, "__origin__", None) is list and a.__args__ and \
                            hasattr(a.__args__[0], "model_fields") and isinstance(value, list):
                        value = [fuzzy_normalize_schema(x, a.__args__[0]) for x in value]
        out[tk] = value
    return out


_FENCE = re.compile(r"^```(?:json|JSON)?\s*|\s*```$")


def extract_json_text(raw: str) -> str:
    """Strip code fences / leading prose; return the outermost JSON object text."""
    s = (raw or "").strip()
    s = _FENCE.sub("", s).strip()
    if s.startswith("{") or s.startswith("["):
        return s
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j > i:
        return s[i:j + 1]
    return s


def parse_llm_json(raw: str, schema: Any) -> Any:
    """Validate an LLM answer against a Pydantic schema, with fence stripping, echoed-schema detection and
    field-name correction. Raises ValueError with a message suitable for a repair retry."""
    cleaned = extract_json_text(raw)
    if not cleaned:
        raise ValueError("empty response")
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"response is not valid JSON ({e.msg} at char {e.pos})")
    if isinstance(parsed, dict) and {"type", "properties", "required"} <= set(parsed):
        raise ValueError("model echoed the JSON Schema instead of data")
    try:
        return schema.model_validate(parsed)
    except ValidationError:
        pass
    try:
        return schema.model_validate(fuzzy_normalize_schema(parsed, schema))
    except ValidationError as e:
        errs = "; ".join(f"{'.'.join(str(x) for x in er['loc'])}: {er['msg']}" for er in e.errors()[:6])
        raise ValueError(f"schema validation failed: {errs}")


# ---------------------------------------------------------------------------------- jsonl logging
def append_jsonl(path: str, items: Iterable[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with _LOCK:
        with open(path, "a", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False, default=str) + "\n")


def log_drops(path: str, items: List[Dict[str, Any]]) -> None:
    append_jsonl(path, items)


def read_jsonl(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def write_json(path: str, obj: Any) -> None:
    path = os.fspath(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    import uuid
    tmp = path + '.'+uuid.uuid4().hex+".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, path)


def read_json(path: str, default: Any = None) -> Any:
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------------- stage checkpoints
STAGE_LIST = ["S0_parse", "S1_bible", "S3_mine", "S4_compose", "S5_cut", "S6_dedup", "S7a_hook", "S7b_cold",
              "S7c_producer", "S8_rank", "S9_export"]


class Checkpoint:
    """File-based stage completion markers inside a run dir (`.done/<stage>`), à la TRM `is_stage_complete`."""

    def __init__(self, run_dir: str):
        self.dir = os.path.join(run_dir, ".done")
        os.makedirs(self.dir, exist_ok=True)

    def is_done(self, stage: str) -> bool:
        return os.path.exists(os.path.join(self.dir, stage))

    def mark(self, stage: str, note: str = "") -> None:
        with open(os.path.join(self.dir, stage), "w", encoding="utf-8") as f:
            f.write(note or time.strftime("%Y-%m-%d %H:%M:%S"))

    def clear(self, stage: str, downstream: bool = True) -> None:
        names = STAGE_LIST[STAGE_LIST.index(stage):] if downstream and stage in STAGE_LIST else [stage]
        for n in names:
            p = os.path.join(self.dir, n)
            if os.path.exists(p):
                os.remove(p)


def parse_ranges(spec: Optional[str]) -> Optional[List[int]]:
    """'1-5,8,10-12' -> [1,2,3,4,5,8,10,11,12]"""
    if not spec:
        return None
    out: List[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return sorted(set(out))
