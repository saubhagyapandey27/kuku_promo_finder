"""Backend protocol, result types, JSON-schema helpers."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol

from ..config import ModelSpec


class CapabilityError(RuntimeError):
    """The model/provider can't honour part of the request shape (response_format, reasoning, temperature...)."""
    retryable = False


class TruncatedError(RuntimeError):
    """Output hit max_tokens before the JSON finished."""
    retryable = False

    def __init__(self, message: str, raw: Optional['RawResult'] = None):
        super().__init__(message)
        self.raw = raw


class BudgetExceeded(RuntimeError):
    retryable = False


@dataclass
class RawResult:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_reasoning: int = 0
    cost: Optional[float] = None          # provider-reported USD (None -> computed from the price table)
    latency_s: float = 0.0
    finish_reason: str = ""
    provider_used: str = ""
    mode: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


class Backend(Protocol):
    def call(self, spec: ModelSpec, system: str, user: str, schema: Any, *, max_tokens: int,
             temperature: Optional[float], mode: str, data_collection: Optional[str]) -> RawResult: ...


MODES = ("json_schema", "json_object")        # ordered: best first


def inline_schema(schema_cls: Any) -> dict:
    """Pydantic JSON schema with $defs inlined and noise removed (title/default/description), additionalProperties
    false everywhere. Providers differ wildly in what they accept; this subset is accepted by all of them."""
    sch = copy.deepcopy(schema_cls.model_json_schema())
    defs = sch.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                return resolve(copy.deepcopy(defs[name]))
            out = {}
            for k, v in node.items():
                if k == 'properties':
                    # A user field named title/default is data, not schema metadata.
                    out[k] = {name: resolve(value) for name, value in v.items()}
                    continue
                if k in ("title", "default", "examples"):
                    continue
                if k == "anyOf" and isinstance(v, list):
                    non_null = [x for x in v if not (isinstance(x, dict) and x.get("type") == "null")]
                    if len(non_null) == 1 and len(v) == 2:           # Optional[X] -> X (field stays non-required)
                        merged = resolve(non_null[0])
                        out.update(merged if isinstance(merged, dict) else {})
                        continue
                out[k] = resolve(v)
            if out.get("type") == "object" and "additionalProperties" not in out:
                out["additionalProperties"] = False
            return out
        if isinstance(node, list):
            return [resolve(x) for x in node]
        return node

    return resolve(sch)


def schema_instructions(schema_cls: Any) -> str:
    """Appended to the system prompt in json_object mode (the provider can't enforce the schema)."""
    return ("\n\nRESPONSE FORMAT: reply with ONE valid JSON object (no markdown, no code fences, no text before or "
            "after) that matches this JSON Schema exactly:\n" + json.dumps(inline_schema(schema_cls)))
