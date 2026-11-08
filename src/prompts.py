"""Prompt files: `prompts/<name>.md` with `## SYSTEM` and `## USER` sections and <<placeholders>>."""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Tuple

PROMPT_DIR = Path(__file__).parent / "prompts"
_PH = re.compile(r"<<(\w+)>>")


@lru_cache(maxsize=None)
def load_prompt(name: str) -> Tuple[str, str]:
    txt = (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")
    m = re.search(r"^## SYSTEM\s*\n(.*?)^## USER\s*\n(.*)$", txt, re.S | re.M)
    if not m:
        raise ValueError(f"prompt {name}: needs '## SYSTEM' and '## USER' sections")
    system=m.group(1).strip()
    if name in ('boundary_audit','final_viewer','final_rank','readiness'):
        from .editorial import POLICY
        system += '\n\n'+POLICY
    return system, m.group(2).strip()


def fill(template: str, **kw) -> str:
    """Replace <<key>> placeholders; unknown placeholders raise, so a typo never silently ships."""
    def rep(m):
        k = m.group(1)
        if k not in kw:
            raise KeyError(f"prompt placeholder <<{k}>> not provided")
        return str(kw[k])
    return _PH.sub(rep, template)


def build(name: str, **kw) -> Tuple[str, str]:
    """-> (system, user). `<<x>>` in the system part is filled too (e.g. group size)."""
    s, u = load_prompt(name)
    s_kw = {k: v for k, v in kw.items() if f"<<{k}>>" in s}
    return fill(s, **s_kw), fill(u, **kw)
