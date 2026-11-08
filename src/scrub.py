"""Judge-render scrubber (plan §5 Renderers).

Stage directions leak things a viewer cannot know: script names, relationships ("her sister"),
secret statuses ("the Duke") and editorial meta-words ("cliffhanger peak"). The scrubber removes
them from SHOT/ACTION/SFX text, delivery tags and speaker labels — never from spoken dialogue.

Order of operations (matters, see tests):
  0. mask character names + relationship-word clergy titles ("Father Thomas") with placeholders
  1. relationship nouns  -> gendered neutral noun ("Alexander's mother" -> "the woman")
  2. spoiler terms       -> "the man" / "the woman" / "the person"
  3. editorial meta-words removed
  4. placeholders -> per-episode visual tags (and "'s" preserved)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .schemas import Show
from .signals import _IMPACT

# ------------------------------------------------------------------------------------------------
# relationship lexicon: noun -> neutral replacement
_FEM = ["mother", "stepmother", "sister", "wife", "aunt", "niece", "widow", "grandmother",
        "fiancée", "fiancee", "mistress"]
_MAS = ["father", "stepfather", "brother", "husband", "uncle", "nephew", "grandfather", "fiancé", "fiance"]
_NEU = ["parent", "cousin", "relative", "sibling", "spouse", "in-law", "kin", "twin"]
_REL: Dict[str, str] = {}
for _w in _FEM:
    _REL[_w] = "woman"
    _REL[_w + "s"] = "women"
for _w in _MAS:
    _REL[_w] = "man"
    _REL[_w + "s"] = "men"
for _w in _NEU:
    _REL[_w] = "person"
    _REL[_w + "s"] = "people"
_REL.update({"family": "people", "families": "people", "parents": "people",
             "daughter": "young woman", "daughters": "young women", "son": "young man", "sons": "young men"})

_NOUN_ALT = "|".join(sorted((re.escape(k) for k in _REL), key=len, reverse=True))
_DET = r"(?:his|her|their|my|our|your|its|the|an?|\w+['’]s)"
_STOP_ADJ = r"(?:and|or|of|to|at|on|in|with|from|by|for|as|toward|towards|into|the|an?|his|her|their|my|our|your|its)"
_ADJ = r"(?:(?!" + _STOP_ADJ + r"\b)[\w-]+\s+)"
# [det + up to 2 adjectives] + relationship noun (+ possessive 's); matches only at a word boundary
_REL_RE = re.compile(
    r"(?<![\w'’-])(?:(?P<det>" + _DET + r")\s+(?P<adj>" + _ADJ + r"{0,2}?))?\b(?P<noun>" + _NOUN_ALT +
    r")\b(?P<poss>['’]s)?",
    re.I,
)
# explicit "former/ex-" relationship forms
_EX_RE = re.compile(r"\b(?:the\s+)?(?:former|ex)[-\s]+(groom|husband|wife|fianc[ée]e?|betrothed)\b", re.I)
_BETROTHED_RE = re.compile(r"\b(?:the\s+|her\s+|his\s+)?betrothed\b", re.I)
# clergy titles that contain a relationship word are names/roles, not relationships ("Father Thomas")
_PROTECT_RE = re.compile(r"\b(?:Father|Mother|Brother|Sister)\s+[A-Z][a-z]{2,}\b")

# editorial meta-language (phrase level first, then words)
_META_PHRASES = [
    (re.compile(r",?\s*\butterly defeated\b", re.I), ""),
    (re.compile(r",?\s*\bas the (?:scene|screen|frame|image)\s+(?:freezes|hangs\b[^.;]*|fades(?: out| to black)?)", re.I), ""),
    (re.compile(r",?\s*\b(?:holding|held|holds)\s+(?:on|for)\s+the\s+cliffhanger\b", re.I), ""),
    (re.compile(r",?\s*\bas the tension\s+(?:peaks|builds|mounts|breaks|crests)\b", re.I), ""),
    (re.compile(r"\b(?:abrupt,?\s+)?(?:suspenseful,?\s+)?cliffhanger\s+(?:cut|beat|moment)\b", re.I), "cut"),
    (re.compile(r"\bfreeze[-\s]frame\b", re.I), "still frame"),
    (re.compile(r"\bin (?:anxious |tense )?suspense\b", re.I), ""),
]
_META_WORDS = re.compile(
    r"\b(?:cliffhanger|climax|climactic|ominous(?:ly)?|dramatic(?:ally)?|suspense(?:ful)?|devastating|"
    r"foreboding)\b[,]?\s*", re.I)
META_TEST_RE = re.compile(
    r"\b(cliffhanger|climax|climactic|ominous(?:ly)?|dramatic(?:ally)?|suspense(?:ful)?|freeze[- ]frame)\b"
    r"|\bthe scene (?:freezes|hangs)\b|\btension (?:peaks|builds)\b", re.I)

_MALE_HINT = re.compile(r"\b(duke|lord|king|prince|heir|sir|baron|earl|count|emperor|man|boy|father|husband)\b", re.I)
_FEMALE_HINT = re.compile(r"\b(duchess|lady|queen|princess|baroness|countess|empress|woman|girl|mother|wife|bride)\b", re.I)


def _ex_repl(m) -> str:
    w = m.group(1).lower()
    if w in ("wife", "fiancee", "fiancée"):
        return "the woman"
    if w == "betrothed":
        return "the person"
    return "the man"


def _fix_articles(s: str) -> str:
    s = re.sub(r"\ba (?=[aeiou])", "an ", s)           # lowercase only: tags like "figure A" stay untouched
    s = re.sub(r"\ban (?=[^aeiou\W])", "a ", s)
    s = re.sub(r"(?i)\b(?:a|an)\s+(the)\b", r"\1", s)      # "past the Matriarch" -> "past the the figure F" -> "past the figure F"
    s = re.sub(r"(?i)\b(the)\s+the\b", r"\1", s)
    s = re.sub(r"\s{2,}", " ", s)
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    s = re.sub(r"([,;])\s*([.])", r"\2", s)
    return s.strip()


@dataclass
class NameEntry:
    surfaces: List[str]                                   # e.g. ["Alexander Ashburn", "Alexander", "Ashburn"]
    tags: List[Tuple[int, int, str]] = field(default_factory=list)   # (from_ep, to_ep, tag)
    gender: str = ""                                       # "m" | "f" | ""

    def tag_for(self, ep: int) -> str:
        best = None
        for a, b, t in self.tags:
            if a <= ep <= b:
                return t
            if best is None or abs(ep - a) < abs(ep - best[0]):
                best = (a, t)
        return best[1] if best else "a person"


class Scrubber:
    def __init__(self, entries: Optional[List[NameEntry]] = None, spoiler_terms: Optional[List[str]] = None,
                 label_map: Optional[Dict[Tuple[str, int], int]] = None, alias_confidence=None):
        self.entries = entries or []
        self.spoiler_terms = [t for t in (spoiler_terms or []) if t.strip()]
        self.label_map = label_map or {}               # (SPEAKER_LABEL_UPPER, ep) -> entry index
        self.alias_confidence=alias_confidence or {}
        self._surf: List[Tuple[str, int]] = []         # (surface, entry_idx) longest first
        for i, e in enumerate(self.entries):
            for s in e.surfaces:
                if len(s.strip()) >= 3:
                    self._surf.append((s.strip(), i))
        self._surf.sort(key=lambda x: -len(x[0]))
        self._surf_re = None
        self._idx_of: Dict[str, int] = {}
        if self._surf:
            for s, i in self._surf:
                self._idx_of.setdefault(s, i)
            alt = "|".join(re.escape(s) for s, _ in self._surf)
            self._surf_re = re.compile(r"(?<![\w])(?P<n>" + alt + r")(?P<poss>['’]s)?(?![\w])")
        self._spoil_re = None
        if self.spoiler_terms:
            alt = "|".join(re.escape(t) for t in sorted(self.spoiler_terms, key=len, reverse=True))
            self._spoil_re = re.compile(r"\b(?:the\s+)?(?:" + alt + r")\b", re.I)

    # ----------------------------------------------------------------- bible adapter
    @classmethod
    def from_bible(cls, bible: dict) -> "Scrubber":
        entries: List[NameEntry] = []
        chars = bible.get("characters", [])
        for ch in chars:
            surfaces = [ch.get("name", "")] + list(ch.get("aliases", []) or [])
            # Recover an unambiguous given name from a personal full name. Never
            # split role titles (High Priest -> High corrupts camera directions).
            words = ch.get('name', '').split()
            generic = _ROLE_WORDS | {'high','royal','dungeon','male','female','third','bald','peasant',
                'village','police','crown','old','loyalist','wedding','the','townsman'}
            if len(words) > 1 and words[0].isalpha() and words[0].lower() not in generic:
                if sum(c.get('name','').split()[:1] == words[:1] for c in chars) == 1:
                    surfaces.append(words[0])
            surfaces = list(dict.fromkeys(x for x in surfaces if x and x.strip()))
            tags = [(int(t.get("from_episode", 1)), int(t.get("to_episode", 9999)), t.get("tag", "a person"))
                    for t in (ch.get("visual_tags") or [])]
            blob = (ch.get("role", "") or "") + " " + (ch.get("description", "") or "")
            entries.append(NameEntry(surfaces=surfaces, tags=tags or [(1, 9999, "a person")],
                                     gender=("f" if _FEMALE_HINT.search(blob) else "m" if _MALE_HINT.search(blob) else "")))
        label_map: Dict[Tuple[str, int], int] = {}
        confidences={}
        for al in bible.get("speaker_aliases", []):
            ci = next((i for i, c in enumerate(chars) if c.get("name") == al.get("character")), None)
            if ci is None:
                continue
            for ep in al.get("episodes", []) or []:
                label_map[(str(al.get("label", "")).upper(), int(ep))] = ci
                confidences[(str(al.get('label','')).upper(),int(ep))]=float(al.get('confidence',.8))
        return cls(entries, bible.get("spoiler_terms", []), label_map, confidences)

    # ----------------------------------------------------------------- core text scrub
    def _tag(self, idx: int, ep: int, cap: bool) -> str:
        t = self.entries[idx].tag_for(ep).strip()
        if not re.match(r"(?i)^(the|a|an)\s", t):
            t = "the " + t
        return t[0].upper() + t[1:] if cap else t

    def scrub(self, text: str, ep: int) -> str:
        if not text:
            return text
        ph: List[Tuple[str, str]] = []                  # (placeholder, payload)

        def mask_protected(m):
            ph.append((f"PRTCT{len(ph)}X", m.group(0)))
            return ph[-1][0]

        # 0a. protect clergy titles with names ("Father Thomas") from the relationship rule
        text = _PROTECT_RE.sub(mask_protected, text)
        # 0b. mask known character names (placeholders survive the relationship step)
        if self._surf_re:
            def mask_name(m):
                i = self._idx_of[m.group("n")]
                ph.append((f"NAMEPH{len(ph)}X", f"N:{i}"))
                return ph[-1][0] + (m.group("poss") or "")

            text = self._surf_re.sub(mask_name, text)
        # 1. relationship nouns
        text = _EX_RE.sub(_ex_repl, text)
        text = _BETROTHED_RE.sub("the person", text)

        def rel(m):
            neutral = _REL[m.group("noun").lower()]
            adj = (m.group("adj") or "").strip()
            poss = m.group("poss") or ""
            pre = m.string[:m.start()]
            cap = (m.start() == 0) or bool(re.search(r"[.!?]\s*$", pre))
            out = "the " + (adj + " " if adj else "") + neutral + poss
            return out[0].upper() + out[1:] if cap else out

        text = _REL_RE.sub(rel, text)
        # 2. spoiler terms
        if self._spoil_re:
            def spoil(m):
                s = m.group(0)
                g = "woman" if _FEMALE_HINT.search(s) and not _MALE_HINT.search(s) else \
                    "man" if _MALE_HINT.search(s) else "person"
                # keep a leading determiner if the term carried one ("the Duke" -> "the man"); a bare term
                # inside a noun phrase ("the late Duke") becomes the bare noun ("the late man")
                return (s[:3] + " " if re.match(r"(?i)the\s", s) else "") + g
            text = self._spoil_re.sub(spoil, text)
        # 3. editorial meta
        for rx, rep in _META_PHRASES:
            text = rx.sub(rep, text)
        text = _META_WORDS.sub("", text)
        # 4. placeholders -> tags / protected titles as-is
        for key, payload in reversed(ph):
            pos = text.find(key)
            if pos < 0:
                continue
            if payload.startswith("N:"):
                start_sentence = pos == 0 or bool(re.search(r"[.!?]\s*$", text[:pos]))
                text = text.replace(key, self._tag(int(payload[2:]), ep, start_sentence), 1)
            else:
                text = text.replace(key, payload, 1)
        return _fix_articles(text)

    # ----------------------------------------------------------------- speaker labels
    def speaker(self, label: str, ep: int) -> str:
        """Visual description for a LINE speaker label (e.g. "ALEXANDER'S MOTHER" -> "elderly woman in robes")."""
        key = (label.strip().upper(), ep)
        if key in self.label_map:
            t = self.entries[self.label_map[key]].tag_for(ep).strip()
            return t if re.match(r"(?i)^(the|a|an)\s", t) else "the " + t
        base = " ".join(w.capitalize() for w in label.strip().split())
        # "ALEXANDER'S MOTHER" -> the speaker IS the mother: drop the possessive owner, keep the neutral noun
        m = re.match(r"^\S+(?:\s+\S+)?['’]s\s+(.+)$", base)
        if m:
            base = m.group(1)
        base = _EX_RE.sub(_ex_repl, base)                       # "The Former Groom" -> "the man"
        # a label that is exactly a known name/title ("Lady Juliana") -> that character's tag
        if self._surf_re:
            whole = self._surf_re.fullmatch(base)
            if whole:
                return self._tag(self._idx_of[whole.group("n")], ep, False)
        words = []
        for w in base.split():
            lw = w.lower().strip(",.")
            if lw in _REL:
                words.append(_REL[lw])
            elif self._surf_re and self._surf_re.fullmatch(w):
                words.append(self.entries[self._idx_of[w]].tag_for(ep).strip())
            else:
                words.append(lw)
        out = " ".join(words).strip()
        out = re.sub(r"(?i)^(the|a|an)\s+", "", out)
        return "the " + out if out else "a person"

    def known_names(self) -> List[str]:
        return [s for s, _ in self._surf]


# ----------------------------------------------------------------------------------------------------
# helpers shared with render.py
def strip_impact_clauses(text: str) -> str:
    """Drop clauses containing impact verbs (slaps, collapses...) so pre-cut events never leak into a header."""
    parts = re.split(r"(?<=[,;])\s+|\s+(?=as |while |before |when )", text)
    keep = [p for p in parts if not _IMPACT.search(p)]
    out = " ".join(keep).strip().rstrip(",;")
    if out and not out.endswith("."):
        out += "."
    return out


def truncate_words(text: str, n: int) -> str:
    w = text.split()
    return text if len(w) <= n else " ".join(w[:n]).rstrip(",;") + "…"


# ----------------------------------------------------------------------------------------------------
_ROLE_WORDS = {"matriarch", "constable", "guard", "lord", "lady", "judge", "nobleman", "priest", "housekeeper",
               "stranger", "soldier", "king", "queen", "duke", "duchess", "footman", "servant", "maid", "captain",
               "general", "bishop", "footmen", "guards", "villager", "innkeeper", "doctor", "nurse", "narrator"}


def infer_entries(show: Show) -> List[NameEntry]:
    """LLM-free fallback (tests, no-bible runs): derive character names from speaker labels that also
    appear capitalised mid-sentence in stage directions. Tags are neutral 'figure N' placeholders."""
    from collections import Counter

    cap: Counter = Counter()
    for e in show.episodes:
        for b in e.blocks:
            if b.kind != "LINE":
                for m in re.finditer(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-z]{2,})\b", b.text):
                    cap[m.group(1)] += 1
    names: List[str] = []
    seen = set()
    for e in show.episodes:
        for b in e.blocks:
            if b.kind == "LINE" and b.speaker and b.speaker not in seen:
                seen.add(b.speaker)
                words = " ".join(w.capitalize() for w in b.speaker.split()).split()
                if any(re.fullmatch(_NOUN_ALT, w.lower().replace("'s", "")) for w in words):
                    continue
                if len(words) == 1 and words[0].lower() not in _ROLE_WORDS and cap.get(words[0], 0) >= 2:
                    names.append(words[0])
                elif len(words) >= 2 and words[0] in ("Lady", "Lord", "Father", "Sir") and cap.get(words[1], 0) >= 1:
                    names.append(" ".join(words))
    entries: List[NameEntry] = []
    by_single: Dict[str, NameEntry] = {}

    def new_tag() -> str:
        n = len(entries)
        return f"figure {chr(65 + n % 26)}{n // 26 or ''}"

    for n in dict.fromkeys(x for x in names if " " not in x):
        ent = NameEntry(surfaces=[n], tags=[(1, 9999, new_tag())])
        entries.append(ent)
        by_single[n] = ent
    for n in dict.fromkeys(x for x in names if " " in x):
        last = n.split()[-1]
        if last in by_single:
            by_single[last].surfaces.append(n)
        else:
            ent = NameEntry(surfaces=[n, last] if len(last) >= 4 else [n], tags=[(1, 9999, new_tag())])
            entries.append(ent)
            by_single[last] = ent
    return entries
