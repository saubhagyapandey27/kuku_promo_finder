"""S0 — tolerant screenplay parser.

Turns raw episode text into structured, time-stamped Blocks. All regexes come from
configs/format.yaml so a new show's format can be fixed in seconds.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from .schemas import Block, Episode, ParseIssue, ParseReport, Scene, Show

_ROOT = Path(__file__).resolve().parent.parent
_STAGE_MARKS = {"O.S.", "OS", "V.O.", "VO", "O.C.", "OC", "CONT'D", "CONTD", "CONT’D",
                "OFF-SCREEN", "OFF SCREEN", "OFFSCREEN", "VOICEOVER", "VOICE OVER", "VOICE-OVER", "FILTERED"}


def load_format(path: Optional[str] = None) -> dict:
    p = Path(path) if path else _ROOT / "configs" / "format.yaml"
    with open(p, "r", encoding="utf-8") as f:
        fmt = yaml.safe_load(f)
    # kind lookup: label (upper) -> kind
    lookup: Dict[str, str] = {}
    for kind, labels in fmt["kinds"].items():
        for lab in labels:
            lookup[lab.upper()] = kind
    fmt["_kind_lookup"] = lookup
    return fmt


# ---------------------------------------------------------------------------------------------

def parse_time(s: str) -> float:
    """'00:46.70' | '01:02:03.5' | '46.7' | '00:46,70' -> seconds."""
    s = s.strip().strip("()[]").replace(",", ".")
    parts = s.split(":")
    try:
        vals = [float(p) for p in parts if p != ""]
    except ValueError:
        raise ValueError(f"bad time {s!r}")
    if not vals:
        raise ValueError(f"empty time {s!r}")
    t = 0.0
    for v in vals:
        t = t * 60.0 + v
    return t


def _clean_label(raw: str) -> Tuple[str, str, str]:
    """'BLOND NOBLEMAN (Shocked, Trembling)' -> (label, speaker, delivery)."""
    label = raw.strip().strip("*: ").strip()
    name = re.split(r"\s*\(", label, maxsplit=1)[0].strip()
    parens = re.findall(r"\(([^)]*)\)", label)
    deliv = []
    for p in parens:
        if p.strip().upper() in _STAGE_MARKS:
            continue
        deliv.append(p.strip())
    return label, name, ", ".join(d for d in deliv if d)


def _kind_for(label_name: str, fmt: dict) -> str:
    return fmt["_kind_lookup"].get(label_name.strip().upper(), "LINE")


def _strip_quotes(t: str) -> str:
    t = t.strip()
    if len(t) >= 2 and t[0] in "\"“'" and t[-1] in "\"”'":
        return t[1:-1].strip()
    return t


def parse_episode(text: str, fmt: dict, source: str = "", ep_hint: Optional[int] = None
                  ) -> Tuple[Episode, List[ParseIssue]]:
    issues: List[ParseIssue] = []
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")

    # --- episode header ---
    ep_no = None
    m = re.search(fmt["episode_number_alt"], text) or re.search(fmt["episode_number"], text)
    if m:
        ep_no = int(m.group(1))
    elif ep_hint is not None:
        ep_no = ep_hint
        issues.append(ParseIssue(level="warn", msg=f"no episode number in text of {source}; using {ep_hint}"))
    else:
        mm = re.search(r"(\d+)", Path(source).stem) if source else None
        ep_no = int(mm.group(1)) if mm else 0
        issues.append(ParseIssue(level="warn", msg=f"no episode number found in {source}; guessed {ep_no}"))
    ml = re.search(fmt["logline"], text)
    logline = ml.group(1).strip() if ml else ""
    mc = re.search(fmt["continuity"], text)
    cont = mc.group(1).strip() if mc else ""

    body = text
    ms = re.search(fmt["script_start"], text)
    if ms:
        body = text[ms.end():]
    me = re.search(fmt["script_end"], body)
    if me:
        body = body[:me.start()]

    hdr_re = re.compile(fmt["block_header"])
    time_re = re.compile(fmt["time_pair"])
    scene_re = re.compile(fmt["scene_marker"])
    head_re = re.compile(fmt["scene_heading"])
    sst_re = re.compile(fmt["scene_start"])
    sen_re = re.compile(fmt["scene_end"])

    lines = body.split("\n")
    scenes: List[Scene] = []
    blocks: List[Block] = []
    n_headers = 0
    cur_scene = -1
    i = 0
    base_line = text[: text.find(body)].count("\n") + 1 if body in text else 1

    def new_scene(num_hint: int = 0):
        nonlocal cur_scene
        cur_scene += 1
        scenes.append(Scene(ep=ep_no, index=cur_scene))

    while i < len(lines):
        raw = lines[i]
        s = raw.strip()
        if not s:
            i += 1
            continue
        if scene_re.match(s):
            new_scene()
            i += 1
            continue
        mh = head_re.match(s)
        if mh:
            if cur_scene < 0:
                new_scene()
            scenes[cur_scene].heading = mh.group(1).strip()
            i += 1
            continue
        mss = sst_re.match(s)
        if mss:
            if cur_scene < 0:
                new_scene()
            try:
                scenes[cur_scene].start = parse_time(mss.group(1))
            except ValueError as e:
                issues.append(ParseIssue(ep=ep_no, level="warn", msg=str(e), line=base_line + i))
            i += 1
            continue
        mse = sen_re.match(s)
        if mse:
            if cur_scene < 0:
                new_scene()
            try:
                scenes[cur_scene].end = parse_time(mse.group(1))
            except ValueError as e:
                issues.append(ParseIssue(ep=ep_no, level="warn", msg=str(e), line=base_line + i))
            i += 1
            continue
        mb = hdr_re.match(s)
        if mb:
            n_headers += 1
            label, name, deliv = _clean_label(mb.group("label"))
            # time line = next non-empty line
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            tm = time_re.search(lines[j]) if j < len(lines) else None
            if not tm:
                issues.append(ParseIssue(ep=ep_no, level="error",
                                         msg=f"block '{label}' has no Start/End line", line=base_line + i))
                i += 1
                continue
            try:
                t0, t1 = parse_time(tm.group("a")), parse_time(tm.group("b"))
            except ValueError as e:
                issues.append(ParseIssue(ep=ep_no, level="error", msg=str(e), line=base_line + j))
                i = j + 1
                continue
            # text lines until blank line / next header / scene marker
            k = j + 1
            while k < len(lines) and not lines[k].strip():
                k += 1
            txt_parts = []
            while k < len(lines) and lines[k].strip() and not hdr_re.match(lines[k].strip()) \
                    and not scene_re.match(lines[k].strip()):
                txt_parts.append(lines[k].strip())
                k += 1
            kind = _kind_for(name, fmt)
            txt = " ".join(txt_parts)
            if kind == "LINE":
                txt = _strip_quotes(txt)
            if cur_scene < 0:
                new_scene()
            blk = Block(id="", ep=ep_no, idx=len(blocks) + 1, kind=kind, label=label,
                        speaker=name if kind == "LINE" else "", delivery=deliv if kind == "LINE" else "",
                        text=txt, t0=t0, t1=t1, scene=cur_scene)
            blk.id = f"E{ep_no:02d}-B{blk.idx:03d}"
            blocks.append(blk)
            i = k
            continue
        i += 1

    # --- validation ---
    if n_headers and len(blocks) < 0.95 * n_headers:
        issues.append(ParseIssue(ep=ep_no, level="error",
                                 msg=f"only {len(blocks)}/{n_headers} block headers parsed with times"))
    if not blocks:
        issues.append(ParseIssue(ep=ep_no, level="error", msg="no blocks parsed"))
    prev = -1.0
    nonmono = 0
    for b in blocks:
        if b.t1 < b.t0 - 1e-6:
            issues.append(ParseIssue(ep=ep_no, level="warn", msg=f"{b.id}: end {b.t1} < start {b.t0}; clamped"))
            b.t1 = b.t0
        if b.t0 < prev - 0.5:
            nonmono += 1
        prev = max(prev, b.t0)
    if nonmono:
        issues.append(ParseIssue(ep=ep_no, level="warn", msg=f"{nonmono} blocks start >0.5 s before an earlier block"))
    # scene bounds (only when scene start/end were present)
    for sc in scenes:
        if sc.end > 0:
            for b in blocks:
                if b.scene == sc.index and (b.t0 < sc.start - 0.3 or b.t1 > sc.end + 0.3):
                    issues.append(ParseIssue(ep=ep_no, level="warn",
                                             msg=f"{b.id} outside scene {sc.index+1} bounds "
                                                 f"({sc.start}-{sc.end})"))
                    break
    dur = max([sc.end for sc in scenes] + [b.t1 for b in blocks] + [0.0])
    ep = Episode(ep=ep_no, logline=logline, continuity=cont, scenes=scenes, blocks=blocks,
                 duration=dur, source=source)
    return ep, issues


def parse_show(texts: List[Tuple[str, str]], fmt: Optional[dict] = None, title: str = ""
               ) -> Tuple[Show, ParseReport]:
    """texts: list of (source_name, raw_text). Returns the Show and a ParseReport."""
    fmt = fmt or load_format()
    eps: List[Episode] = []
    rep = ParseReport()
    seen = set()
    for src, txt in texts:
        ep, iss = parse_episode(txt, fmt, source=src)
        rep.issues.extend(iss)
        if ep.ep in seen:
            rep.issues.append(ParseIssue(ep=ep.ep, level="error", msg=f"duplicate episode number ({src})"))
            continue
        seen.add(ep.ep)
        eps.append(ep)
    eps.sort(key=lambda e: e.ep)
    show = Show(title=title, episodes=eps)
    rep.episodes = len(eps)
    rep.blocks = sum(len(e.blocks) for e in eps)
    rep.lines = sum(1 for e in eps for b in e.blocks if b.kind == "LINE")
    for e in eps:
        rep.per_episode.append({
            "ep": e.ep, "source": e.source, "blocks": len(e.blocks),
            "lines": sum(1 for b in e.blocks if b.kind == "LINE"),
            "scenes": len(e.scenes), "duration_s": round(e.duration, 2),
        })
    # gaps in episode numbering
    nums = [e.ep for e in eps]
    if nums:
        missing = [n for n in range(min(nums), max(nums) + 1) if n not in set(nums)]
        if missing:
            rep.issues.append(ParseIssue(level="warn", msg=f"missing episode numbers: {missing}"))
    return show, rep
