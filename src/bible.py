"""S1 — series bible (P1) + code checks.

After the call, code verifies (plan §5 S1):
  * every speaker label in every episode resolves to a character (else the scrubber's label fallback is used);
  * visual tags are unique within an episode;
  * tags pass the scrubber unchanged (no names, relationship nouns, spoiler terms or meta-words inside a tag).
On failure: one repair retry with the errors; if it still fails, offending tags are neutralised and the run is flagged.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from . import prompts
from .render import render_compact
from .schemas import SeriesBible, Show
from .scrub import META_TEST_RE, Scrubber, _REL, _REL_RE
from .util import write_json


# ------------------------------------------------------------------------------ prompt inputs
def label_inventory(show: Show) -> Dict[str, dict]:
    inv: Dict[str, dict] = {}
    for ep in show.episodes:
        for b in ep.blocks:
            if b.kind != "LINE":
                continue
            d = inv.setdefault(b.speaker.upper(), {"eps": set(), "n": 0})
            d["eps"].add(ep.ep)
            d["n"] += 1
    return inv


def _ranges(nums: List[int]) -> str:
    nums = sorted(nums)
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(f"{nums[i]}" if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ",".join(out)


def inventory_text(inv: Dict[str, dict]) -> str:
    rows = sorted(inv.items(), key=lambda kv: -kv[1]["n"])
    return "\n".join(f"{lab}: episodes {_ranges(list(d['eps']))} ({d['n']} lines)" for lab, d in rows)


def episode_notes_text(show: Show) -> str:
    return "\n".join(f"E{e.ep}: LOGLINE {e.logline} | CONTINUITY {e.continuity}" for e in show.episodes)


# ------------------------------------------------------------------------------ normalisation
def clean_spoiler_terms(terms: List[str]) -> List[str]:
    out: List[str] = []
    for t in terms:
        t = re.sub(r"^\s*the\s+", "", (t or "").strip(), flags=re.I).strip(" .,\"'")
        if len(t) >= 3 and t.lower() not in {x.lower() for x in out}:
            out.append(t)
    return out


def bible_to_dict(bible: SeriesBible, show: Show, inv: Optional[Dict[str, dict]] = None) -> dict:
    """Plain dict with speaker_aliases expanded (empty `episodes` = every episode the label appears in)."""
    inv = inv or label_inventory(show)
    d = bible.model_dump()
    d["spoiler_terms"] = clean_spoiler_terms(d.get("spoiler_terms", []))
    for al in d.get("speaker_aliases", []):
        key = str(al.get("label", "")).upper()
        if not al.get("episodes") and key in inv:
            al["episodes"] = sorted(inv[key]["eps"])
    return d


# ------------------------------------------------------------------------------ validation
_TITLE_WORDS = {"lady", "lord", "sir", "dame", "duke", "duchess", "king", "queen", "prince", "princess", "father",
                "mother", "brother", "sister", "mister", "madam", "miss", "mrs", "the", "of", "and", "old", "young"}


_GENERIC_NOUNS = {"man", "woman", "stranger", "guard", "priest", "matriarch", "patriarch", "nobleman", "noblewoman",
                  "figure", "voice", "soldier", "servant", "maid", "footman", "captain", "knight", "officer", "child",
                  "boy", "girl", "gentleman", "guards", "bride", "groom", "narrator", "villager", "innkeeper"}


def lower_vocab(show: Show) -> set:
    """Every word that appears in lowercase somewhere in the screenplay. A real character name ("Juliana") is never
    written in lowercase, while role words ("housekeeper", "thug", "constable", "gown", "cap") always are, so this
    separates names from appearance/role vocabulary without a hand-kept list."""
    vocab = set()
    for ep in show.episodes:
        for b in ep.blocks:
            vocab.update(re.findall(r"\b[a-z][a-z'-]{2,}\b", b.text or ""))
    return vocab


def _is_common(token: str, vocab: set) -> bool:
    """True if the token (or a trivial inflection of it: blond/blonde, fighter/fighters) is written in lowercase in
    the screenplay, i.e. it is ordinary vocabulary and not a proper name."""
    t = token.lower()
    forms = {t, t + "s", t + "e", t + "es", t + "ed", t + "d", t + "er", t + "ers", t + "ing"}
    if t.endswith("s") or t.endswith("e"):
        forms.add(t[:-1])
    if t.endswith("ed") or t.endswith("er"):
        forms.add(t[:-2])
    return any(f in vocab for f in forms)


def true_names(bible: dict, vocab: Optional[set] = None) -> List[str]:
    """Proper names a visual tag must not contain: each character's name tokens plus aliases that share a token with
    the name ("Lady Juliana"). Speaker-label descriptors such as "Lady in Gold Gown" or "Bearded Man" are appearance
    words, which a visual tag is supposed to use, so they are not names (the first live run rejected every good
    tag, "scarred bearded warrior in coat" included, because they were treated as names)."""
    out: List[str] = []
    if vocab is not None:
        for ch in bible.get("characters", []):
            for s in [ch.get("name", "")] + list(ch.get("aliases", []) or []):
                for t in re.findall(r"[A-Za-z][A-Za-z'-]+", s or ""):
                    if len(t) >= 3 and t.lower() not in _TITLE_WORDS and not _is_common(t, vocab):
                        out.append(t)
        return list(dict.fromkeys(out))
    for ch in bible.get("characters", []):
        name = ch.get("name", "") or ""
        if any(w.lower() in _GENERIC_NOUNS for w in re.findall(r"[A-Za-z]+", name)):
            continue            # a descriptor-name ("Mysterious Bearded Man", "Royal Guard"): no real name to protect yet
        toks = [t for t in re.findall(r"[A-Za-z][A-Za-z'-]+", name) if t.lower() not in _TITLE_WORDS and len(t) >= 3]
        out.extend(toks)
        if len(toks) > 1:
            out.append(" ".join(toks))
        low = {t.lower() for t in toks}
        for al in ch.get("aliases", []) or []:
            atoks = [t for t in re.findall(r"[A-Za-z][A-Za-z'-]+", al or "") if t.lower() not in _TITLE_WORDS and len(t) >= 3]
            if atoks and low & {t.lower() for t in atoks}:
                out.extend(atoks)
    return list(dict.fromkeys(out))


def tag_problems(bible: dict, last_ep: int, vocab: Optional[set] = None) -> List[str]:
    """Visual-tag problems (uniqueness per episode, scrub-cleanliness)."""
    probs: List[str] = []
    names = true_names(bible, vocab)
    per_ep: Dict[int, Dict[str, str]] = defaultdict(dict)
    for ch in bible.get("characters", []):
        for vt in ch.get("visual_tags", []) or []:
            tag = (vt.get("tag") or "").strip()
            lo, hi = int(vt.get("from_episode", 1)), min(int(vt.get("to_episode", last_ep)), last_ep)
            if not tag:
                probs.append(f"{ch['name']}: empty visual tag for E{lo}-E{hi}")
                continue
            if _REL_RE.search(tag):
                probs.append(f"{ch['name']}: tag '{tag}' contains a relationship word")
            if META_TEST_RE.search(tag):
                probs.append(f"{ch['name']}: tag '{tag}' contains an editorial word")
            for n in names:
                if re.search(rf"(?<!\w){re.escape(n)}(?!\w)", tag, re.I):
                    probs.append(f"{ch['name']}: tag '{tag}' contains the name/term '{n}'")
                    break
            for t in bible.get("spoiler_terms", []):
                if t and re.search(rf"\b{re.escape(t)}\b", tag, re.I):
                    probs.append(f"{ch['name']}: tag '{tag}' contains spoiler term '{t}'")
            if len(tag.split()) > 9:
                probs.append(f"{ch['name']}: tag '{tag}' is too long")
            for e in range(max(1, lo), hi + 1):
                prev = per_ep[e].get(tag.lower())
                if prev and prev != ch["name"]:
                    probs.append(f"E{e}: tag '{tag}' is used for both {prev} and {ch['name']}")
                per_ep[e][tag.lower()] = ch["name"]
    # de-duplicate messages
    return list(dict.fromkeys(probs))


def label_resolution(bible: dict, show: Show, inv: Optional[Dict[str, dict]] = None) -> Tuple[float, List[str]]:
    """Share of dialogue lines whose speaker label maps to a character, and the unresolved label list."""
    inv = inv or label_inventory(show)
    resolved = {(str(a["label"]).upper(), int(e)) for a in bible.get("speaker_aliases", []) for e in a.get("episodes", [])}
    names = {c["name"].upper() for c in bible.get("characters", [])}
    for c in bible.get("characters", []):
        names.update(x.upper() for x in c.get("aliases", []) or [])
    tot = ok = 0
    unresolved: Dict[str, int] = defaultdict(int)
    for ep in show.episodes:
        for b in ep.blocks:
            if b.kind != "LINE":
                continue
            tot += 1
            key = (b.speaker.upper(), ep.ep)
            if key in resolved or b.speaker.upper() in names:
                ok += 1
            else:
                unresolved[b.speaker.upper()] += 1
    top = [f"{k} ({v} lines)" for k, v in sorted(unresolved.items(), key=lambda kv: -kv[1])[:12]]
    return (ok / tot if tot else 1.0), top


_SPOIL_NOUN_GENDER = {"duke": "man", "duchess": "woman", "heir": "person", "king": "man", "queen": "woman",
                      "prince": "man", "princess": "woman", "lord": "man", "lady": "woman", "emperor": "man",
                      "empress": "woman", "baron": "man", "baroness": "woman", "earl": "man", "countess": "woman"}


def _fix_one_tag(tag: str, bible: dict, names: List[str]) -> str:
    """Deterministic rewrite of a visual tag so it keeps its appearance words but loses leaks: a spoiler noun becomes
    a plain man/woman, a spoiler adjective ("ducal") or a name is dropped, a relationship noun becomes man/woman."""
    out = tag
    for t in sorted({x for x in bible.get("spoiler_terms", []) if x}, key=len, reverse=True):
        core = re.sub(r"(?i)^the\s+", "", t.strip())
        rep = _SPOIL_NOUN_GENDER.get(core.lower(), "")
        out = re.sub(rf"(?i)\b{re.escape(core)}\b", rep, out)
    for n in names:
        out = re.sub(rf"(?<!\w){re.escape(n)}(?!\w)", "", out, flags=re.I)

    out = _REL_RE.sub(lambda m: _REL.get(m.group("noun").lower(), "person"), out)
    out = META_TEST_RE.sub("", out)
    out = re.sub(r"\s{2,}", " ", out).strip(" ,;")
    out = re.sub(r"(?i)\b(man|woman|person)\s+\1\b", r"\1", out)
    return out


def autofix_tags(bible: dict, last_ep: int, vocab: Optional[set] = None) -> int:
    """Rewrite every tag that fails validation (see _fix_one_tag). Returns how many tags changed."""
    names = true_names(bible, vocab)
    fixed = 0
    for ch in bible.get("characters", []):
        for vt in ch.get("visual_tags", []) or []:
            tag = (vt.get("tag") or "").strip()
            if not tag:
                continue
            probe = {"characters": [{"name": ch["name"], "visual_tags": [{**vt, "tag": tag}]}],
                     "spoiler_terms": bible.get("spoiler_terms", [])}
            own = [p for p in tag_problems(probe, last_ep, vocab) if "used for both" not in p]
            if not own:
                continue
            new = _fix_one_tag(tag, bible, names)
            if new and new != tag and len(new.split()) >= 2:
                vt["tag"] = new
                fixed += 1
    return fixed


def neutralise_bad_tags(bible: dict, last_ep: int, vocab: Optional[set] = None) -> int:
    """Last resort after a failed repair: replace offending tags with a neutral one so nothing leaks. Returns count."""
    fixed = 0
    probs = tag_problems(bible, last_ep, vocab)
    if not probs:
        return 0
    bad_names = set()
    for p in probs:
        m = re.match(r"(.+?): tag '(.+?)'", p)
        if m:
            bad_names.add((m.group(1), m.group(2)))
    letter = iter("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    for ch in bible.get("characters", []):
        for vt in ch.get("visual_tags", []) or []:
            if (ch["name"], vt.get("tag", "")) in bad_names:
                vt["tag"] = f"figure {next(letter, 'Z')}"
                fixed += 1
    return fixed


# ------------------------------------------------------------------------------ stage
def build_bible(show: Show, router, log=print, force_chain=None) -> Tuple[SeriesBible, dict, dict]:
    """-> (SeriesBible, bible_dict, meta{flags, label_share, unresolved, tag_problems, model})"""
    inv = label_inventory(show)
    last_ep = max(e.ep for e in show.episodes)
    system, user = prompts.build(
        "p1_bible", show_title=show.title or "(untitled series)", episode_notes=episode_notes_text(show),
        label_inventory=inventory_text(inv), digest_all=render_compact(show))
    flags: List[str] = []
    res = router.review("bible", system, user, SeriesBible, stage="S1_bible", item_id="bible", chain=force_chain)
    bible_d = bible_to_dict(res.obj, show, inv)
    vocab = lower_vocab(show)
    probs = tag_problems(bible_d, last_ep, vocab)
    share, unresolved = label_resolution(bible_d, show, inv)
    model_used = res.spec.label
    models_used = [model_used]
    if probs:            # free deterministic rewrite first (keeps the appearance words); an LLM repair only if it fails
        n_fix = autofix_tags(bible_d, last_ep, vocab)
        if n_fix:
            flags.append(f"bible_tags_autofixed:{n_fix}")
        probs = tag_problems(bible_d, last_ep, vocab)
    if probs or share < 0.80:
        log(f"  [bible] {len(probs)} tag problem(s), label resolution {share:.0%}; asking for one repair")
        fix = ["Your bible has problems. Fix ALL of them and return the complete corrected bible:"]
        fix += [f"- {p}" for p in probs[:25]]
        if share < 0.80:
            fix.append(f"- only {share:.0%} of dialogue lines have a speaker label mapped to a character in "
                       f"speaker_aliases; unmapped labels: {', '.join(unresolved)}. Map every label for every episode "
                       f"it appears in.")
        res2 = router.review("bible", system, user + "\n\n---\n" + "\n".join(fix), SeriesBible,
                                    stage="S1_bible", item_id="bible_repair", chain=force_chain)
        models_used.append(res2.spec.label)
        d2 = bible_to_dict(res2.obj, show, inv)
        autofix_tags(d2, last_ep, vocab)
        p2 = tag_problems(d2, last_ep, vocab)
        s2, u2 = label_resolution(d2, show, inv)
        if len(p2) <= len(probs) and s2 >= share - 0.02:
            bible_d, probs, share, unresolved, res = d2, p2, s2, u2, res2
            model_used = res2.spec.label
            bible = res2.obj
        flags.append("bible_repaired")
    if probs:                                    # only what is still wrong becomes a neutral "figure X"
        n = neutralise_bad_tags(bible_d, last_ep, vocab)
        flags.append(f"bible_tags_neutralised:{n}")
        probs = tag_problems(bible_d, last_ep, vocab)
    if share < 0.80:
        flags.append(f"bible_label_resolution_low:{share:.2f}")
    bible_obj = SeriesBible.model_validate(bible_d)
    meta = {"flags": flags, "label_resolution": round(share, 3), "unresolved_labels": unresolved,
            "tag_problems": probs, "model": model_used, "models": sorted(set(models_used)), "router_flags": res.flags}
    return bible_obj, bible_d, meta


def bible_for_prompt(bible_d: dict) -> str:
    """Compact bible JSON for the miner / composer / producer (no visual tags or per-episode arc)."""
    import json
    keep = {
        "premise": bible_d.get("premise"), "tropes": bible_d.get("tropes"),
        "acquisition_angles": bible_d.get("acquisition_angles"),
        "characters": [{k: c.get(k) for k in ("name", "aliases", "role", "description", "relationships", "secret",
                                              "secret_revealed_ep") if c.get(k)}
                       for c in bible_d.get("characters", [])],
        "speaker_aliases": [{"label": a["label"], "character": a["character"], "episodes": _ranges(a.get("episodes", []))}
                            for a in bible_d.get("speaker_aliases", [])],
        "reveals": bible_d.get("reveals"), "sensitive_content": bible_d.get("sensitive_content"),
        "continuity_issues": bible_d.get("continuity_issues"),
    }
    return json.dumps(keep, ensure_ascii=False, separators=(",", ":"))


def arc_text(bible_d: dict, eps: Optional[List[int]] = None) -> str:
    rows = []
    for a in bible_d.get("episode_arc", []):
        if eps is None or a["episode"] in eps:
            rows.append(f"E{a['episode']}: {a.get('summary', '')} [ends on: {a.get('ends_on', '')}]")
    return "\n".join(rows)
