"""S0 — input loader: folder | zip | single combined file; .txt/.md/.docx/.pdf.

Returns a list of (source_name, text), one entry per episode. Combined files are split on the
episode-header regex from configs/format.yaml.
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

from .parse import load_format

_TEXT_EXT = {".txt", ".md", ".text", ".fountain"}


def _read_docx(data: bytes) -> str:
    try:
        import docx  # python-docx
    except ImportError as exc:
        raise ValueError("DOCX input needs an optional reader: pip install python-docx==1.2.0. TXT/MD files, including ZIPs of them, need no extra reader.") from exc
    d = docx.Document(io.BytesIO(data))
    return "\n".join(p.text for p in d.paragraphs)


def _read_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("PDF input needs an optional reader: pip install pypdf==7.0.0. TXT/MD files, including ZIPs of them, need no extra reader.") from exc
    r = PdfReader(io.BytesIO(data))
    return "\n".join((pg.extract_text() or "") for pg in r.pages)


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _read_bytes(name: str, data: bytes) -> Optional[str]:
    ext = Path(name).suffix.lower()
    if ext in _TEXT_EXT or ext == "":
        return _decode(data)
    if ext == ".docx":
        return _read_docx(data)
    if ext == ".pdf":
        return _read_pdf(data)
    return None


def _natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _split_combined(name: str, text: str, split_re: str) -> List[Tuple[str, str]]:
    starts = [m.start() for m in re.finditer(split_re, text)]
    if len(starts) <= 1:
        return [(name, text)]
    starts.append(len(text))
    out = []
    for k in range(len(starts) - 1):
        out.append((f"{name}#part{k+1}", text[starts[k]:starts[k + 1]]))
    return out


def load_texts(path: str, fmt: Optional[dict] = None) -> List[Tuple[str, str]]:
    """Load raw episode texts from a folder, zip file or single file."""
    fmt = fmt or load_format()
    split_re = fmt["episode_split"]
    p = Path(path)
    raw: List[Tuple[str, str]] = []
    if p.is_dir():
        files = sorted([f for f in p.rglob("*") if f.is_file()], key=lambda f: _natural_key(f.name))
        for f in files:
            t = _read_bytes(f.name, f.read_bytes())
            if t is not None and t.strip():
                raw.append((f.name, t))
    elif p.suffix.lower() == ".zip":
        with zipfile.ZipFile(p) as z:
            names = sorted([n for n in z.namelist() if not n.endswith("/") and "__MACOSX" not in n],
                           key=_natural_key)
            for n in names:
                t = _read_bytes(n, z.read(n))
                if t is not None and t.strip():
                    raw.append((Path(n).name, t))
    elif p.is_file():
        t = _read_bytes(p.name, p.read_bytes())
        if t:
            raw.append((p.name, t))
    else:
        raise FileNotFoundError(path)
    out: List[Tuple[str, str]] = []
    for name, text in raw:
        out.extend(_split_combined(name, text, split_re))
    return out


def load_bytes_upload(files: List[Tuple[str, bytes]], fmt: Optional[dict] = None) -> List[Tuple[str, str]]:
    """For multipart upload: list of (filename, bytes) including ZIPs."""
    fmt = fmt or load_format()
    raw: List[Tuple[str, str]] = []
    for name, data in files:
        if name.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for n in sorted(z.namelist(), key=_natural_key):
                    if n.endswith("/") or "__MACOSX" in n:
                        continue
                    t = _read_bytes(n, z.read(n))
                    if t and t.strip():
                        raw.append((Path(n).name, t))
        else:
            t = _read_bytes(name, data)
            if t and t.strip():
                raw.append((name, t))
    out: List[Tuple[str, str]] = []
    for name, text in raw:
        out.extend(_split_combined(name, text, fmt["episode_split"]))
    return out
