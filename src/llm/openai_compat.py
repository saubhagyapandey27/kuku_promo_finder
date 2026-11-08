"""OpenAI-compatible backend: OpenRouter (default) or any custom endpoint (e.g. APIs Kuku provides)."""
from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

from ..config import ModelSpec
from ..util import ModerationBlock
from .base import CapabilityError, RawResult, TruncatedError, inline_schema, schema_instructions

OPENROUTER_URL = "https://openrouter.ai/api/v1"


def _err_text(e: Exception) -> str:
    body = getattr(e, "body", None)
    return f"{e} | {body}" if body else str(e)


class OpenAICompatBackend:
    def __init__(self, credentials=None):
        self._clients: Dict[tuple, Any] = {}
        self.credentials = credentials

    def close(self):
        clients, self._clients = self._clients, {}
        for client in clients.values():
            try: client.close()
            except Exception: pass

    def _client(self, spec: ModelSpec):
        from openai import OpenAI
        key_env = spec.api_key_env or ("OPENROUTER_API_KEY" if spec.provider == "openrouter" else "CUSTOM_API_KEY")
        base = spec.base_url or OPENROUTER_URL
        k = (base, key_env)
        if k not in self._clients:
            key = self.credentials.get(key_env) if self.credentials is not None else os.environ.get(key_env)
            if not key:
                raise CapabilityError(f"{key_env} is not set (put it in .env)")
            self._clients[k] = OpenAI(api_key=key, base_url=base, timeout=600.0, max_retries=0,
                                      default_headers={"HTTP-Referer": "https://github.com/kuku-promo-finder",
                                                       "X-Title": "kuku-promo-finder"})
        return self._clients[k]

    # ------------------------------------------------------------------ request building
    def build_request(self, spec: ModelSpec, system: str, user: str, schema: Any, *, max_tokens: int,
                      temperature: Optional[float], mode: str, data_collection: Optional[str]) -> dict:
        if spec.price_out > 0.50 or (not spec.free and not spec.price_known):
            raise ValueError('model output price exceeds $0.50/M or price is unknown')
        # Some routes accept response_format while only loosely following its schema.
        # Give the model the field contract explicitly in both modes as well.
        sys_txt = system + schema_instructions(schema)
        kw: Dict[str, Any] = dict(model=spec.id, max_tokens=max_tokens,
                                  messages=[{"role": "system", "content": sys_txt}, {"role": "user", "content": user}])
        if temperature is not None and spec.supports_temperature:
            kw["temperature"] = temperature
        if mode == "json_schema":
            kw["response_format"] = {"type": "json_schema", "json_schema": {
                "name": schema.__name__, "strict": False, "schema": inline_schema(schema)}}
        else:
            kw["response_format"] = {"type": "json_object"}
        extra: Dict[str, Any] = {}
        if spec.provider == "openrouter":
            extra["usage"] = {"include": True}
            if spec.effort and spec.effort != "none":
                extra["reasoning"] = {"effort": spec.effort}
            prov: Dict[str, Any] = {"allow_fallbacks": True}
            if data_collection:
                prov["data_collection"] = data_collection
            if mode == "json_schema":
                prov["require_parameters"] = True
            # Bind both rates to the reservation's prices; routing headroom can invalidate a cost bound.
            prov['max_price'] = {'prompt': spec.price_in, 'completion': spec.price_out}
            extra["provider"] = prov
        elif spec.effort and spec.effort != "none":
            extra["reasoning_effort"] = spec.effort         # plain OpenAI-compatible servers
        if extra:
            kw["extra_body"] = extra
        return kw

    # ------------------------------------------------------------------ call
    def call(self, spec: ModelSpec, system: str, user: str, schema: Any, *, max_tokens: int,
             temperature: Optional[float], mode: str, data_collection: Optional[str]) -> RawResult:
        import openai
        client = self._client(spec)
        kw = self.build_request(spec, system, user, schema, max_tokens=max_tokens, temperature=temperature,
                                mode=mode, data_collection=data_collection)
        t0 = time.time()
        try:
            resp = client.chat.completions.create(**kw)
        except openai.APIStatusError as e:
            self._raise_classified(e)
            raise
        except openai.APIConnectionError as e:
            raise RuntimeError(f"connection error: {e}") from e
        dt = time.time() - t0
        me = getattr(resp, "model_extra", None) or {}
        err = getattr(resp, "error", None) or me.get("error")      # OpenRouter can return HTTP 200 + {"error": ...}
        if err and not getattr(resp, "choices", None):
            msg = str(err)
            if "moderation" in msg.lower() or "flagged" in msg.lower():
                raise ModerationBlock(msg, err)
            raise RuntimeError(f"provider error: {msg}")
        if not resp.choices:
            raise RuntimeError("empty response (no choices)")
        ch = resp.choices[0]
        finish = getattr(ch, "finish_reason", "") or ""
        if finish == "content_filter":
            raise ModerationBlock("finish_reason=content_filter")
        text = (ch.message.content or "") if ch.message else ""
        u = resp.usage
        cost = None
        t_in = t_out = t_reason = 0
        if u is not None:
            t_in = int(getattr(u, "prompt_tokens", 0) or 0)
            t_out = int(getattr(u, "completion_tokens", 0) or 0)
            extra = getattr(u, "model_extra", None) or {}
            cost = extra.get("cost", getattr(u, "cost", None))
            det = getattr(u, "completion_tokens_details", None)
            t_reason = int(getattr(det, "reasoning_tokens", 0) or 0) if det else 0
        prov_used = str(me.get("provider", "")) if me else ""
        raw = RawResult(text=text, tokens_in=t_in, tokens_out=t_out, tokens_reasoning=t_reason,
                         cost=float(cost) if cost is not None else None, latency_s=dt, finish_reason=finish,
                         provider_used=prov_used, mode=mode)
        if finish == 'length':
            raise TruncatedError('output truncated at max_tokens', raw=raw)
        if not text.strip():
            refusal = getattr(ch.message, 'refusal', None) if ch.message else None
            if refusal:
                raise ModerationBlock(f'refusal: {refusal}')
            raise RuntimeError('empty response (no content)')
        return raw

    @staticmethod
    def _raise_classified(e) -> None:
        code = getattr(e, "status_code", None)
        txt = _err_text(e)
        low = txt.lower()
        if code == 403 and ("moderation" in low or "flagged" in low or "reasons" in low):
            raise ModerationBlock(txt, getattr(e, "body", None)) from e
        if code in (400, 404, 422) and any(k in low for k in (
                "response_format", "json_schema", "structured", "no endpoints found", "0 endpoints",
                "guardrail restrictions", "require_parameters",
                "does not support", "not support", "unsupported", "tool", "temperature", "reasoning")):
            raise CapabilityError(txt) from e
        if code == 402:
            raise RuntimeError(f"402 payment required: out of OpenRouter credit? {txt[:200]}") from e
        if code in (401, 403):
            raise RuntimeError(f"auth/permission error ({code}): {txt[:300]}") from e
        # 429 / 5xx stay generic; util.is_transient_error() recognises the status code in the message
        raise RuntimeError(f"{code} {txt[:400]}") from e
