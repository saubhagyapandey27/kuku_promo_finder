"""Gemini backend (google-genai, AI Studio free tier)."""
from __future__ import annotations

import os
import time
from typing import Any, Optional

from ..config import ModelSpec
from ..util import ModerationBlock, PersistentRateLimit
from .base import CapabilityError, RawResult, TruncatedError, inline_schema

_LEVEL = {"minimal": "MINIMAL", "none": "MINIMAL", "low": "LOW", "medium": "MEDIUM", "high": "HIGH"}


class GeminiBackend:
    def __init__(self, credentials=None):
        self._client = None
        self.credentials = credentials

    def close(self):
        client, self._client = self._client, None
        if client:
            try: client.close()
            except Exception: pass

    def _c(self):
        if self._client is None:
            from google import genai
            key = (self.credentials.get('GEMINI_API_KEY') if self.credentials is not None else
                   os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
            if not key:
                raise CapabilityError("GEMINI_API_KEY is not set (put it in .env)")
            # AI Studio (API key) mode, pinned explicitly: a machine-wide GOOGLE_GENAI_USE_VERTEXAI=true would otherwise
            # silently switch the SDK to Vertex and reject the key with "401 API keys are not supported by this API".
            from google.genai import types as _t
            timeout_ms = int(float(os.environ.get("PF_GEMINI_TIMEOUT_S", "120")) * 1000)   # a hung request must become an error
            self._client = genai.Client(api_key=key, vertexai=False, http_options=_t.HttpOptions(timeout=timeout_ms))
        return self._client

    def call(self, spec: ModelSpec, system: str, user: str, schema: Any, *, max_tokens: int,
             temperature: Optional[float], mode: str, data_collection: Optional[str]) -> RawResult:
        from google.genai import errors as gerr
        from google.genai import types
        safety = [types.SafetySetting(category=c, threshold="BLOCK_ONLY_HIGH") for c in (
            "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH", "HARM_CATEGORY_SEXUALLY_EXPLICIT",
            "HARM_CATEGORY_DANGEROUS_CONTENT")]
        cfg = dict(system_instruction=system, max_output_tokens=max_tokens, safety_settings=safety,
                   response_mime_type="application/json", response_json_schema=inline_schema(schema))
        if temperature is not None:
            cfg["temperature"] = temperature
        level = _LEVEL.get((spec.effort or "medium").lower())
        if level:
            cfg["thinking_config"] = types.ThinkingConfig(thinking_level=level)
        t0 = time.time()
        try:
            resp = self._c().models.generate_content(model=spec.id, contents=user,
                                                     config=types.GenerateContentConfig(**cfg))
        except gerr.APIError as e:
            msg = f"{getattr(e, 'code', '')} {getattr(e, 'status', '')} {getattr(e, 'message', e)}"
            low = msg.lower()
            if "perday" in low.replace(" ", "").replace("_", "") or "per day" in low:
                raise PersistentRateLimit(f"Gemini daily quota exhausted: {msg[:300]}") from e
            if getattr(e, "code", None) == 400 and any(k in low for k in ("schema", "thinking", "response_mime", "not supported")):
                raise CapabilityError(msg) from e
            raise RuntimeError(msg) from e
        dt = time.time() - t0
        pf = getattr(resp, "prompt_feedback", None)
        if pf is not None and getattr(pf, "block_reason", None):
            raise ModerationBlock(f"prompt blocked: {pf.block_reason}")
        cand = (resp.candidates or [None])[0]
        fr = str(getattr(cand, "finish_reason", "") or "") if cand else ""
        if cand is not None and any(k in fr.upper() for k in ("SAFETY", "PROHIBITED", "BLOCKLIST", "SPII")):
            raise ModerationBlock(f"blocked: {fr}")
        text = resp.text or ""
        um = resp.usage_metadata
        t_in = int(getattr(um, "prompt_token_count", 0) or 0) if um else 0
        t_out = int(getattr(um, "candidates_token_count", None) or getattr(um, "response_token_count", 0) or 0) if um else 0
        t_think = int(getattr(um, "thoughts_token_count", 0) or 0) if um else 0
        raw = RawResult(text=text, tokens_in=t_in, tokens_out=t_out + t_think, tokens_reasoning=t_think,
                         cost=0.0, latency_s=dt, finish_reason=fr, provider_used="google-ai-studio", mode="json_schema")
        if 'MAX_TOKENS' in fr.upper():
            raise TruncatedError('Gemini output truncated at max_output_tokens', raw=raw)
        if not text.strip():
            raise RuntimeError('empty response (no text)')
        return raw
