# Microdrama Promo Finder

Built by **Saubhagya Pandey (Roll No. 230939)** for Problem 1 of the Kuku AI Generalist Assignment. It reads timestamped microdrama screenplays and returns ranked 20–60 second promo cuts — exact ordered episode timestamps, the opening hook, the cliffhanger, and one line on why each works. On the bundled show the accepted result is **29 promos from 50 episodes**.

**Live site (GitHub Pages):** https://saubhagyapandey27.github.io/kuku_promo_finder/

## How it works

The pipeline is a **blind-first, proof-checked** orchestration (`src/runner.py`). The model never writes timestamps or final cuts directly — it references block IDs and judgment, and deterministic code computes every cut boundary, so a hallucinated timestamp cannot ship.

```
screenplay folder / ZIP
        │
        ▼
  S0 parse ── format.yaml regexes → time-stamped Blocks (E01-B001…)
        │
        ▼
  S1 bible ── series bible: characters, arcs, speaker aliases, spoiler terms
        │
        ▼
  S3 mine ── window miner names block IDs (≤2/episode); code computes timestamps
        │
        ▼
  Boundary ── deterministic legal cuts per seed, fingerprint-checked
        │
        ▼
  Opening screen ── first-3s hook, single-flight deduped by content hash
        │
        ▼
  Semantic review ── clip-only cold viewer (no title/rationale leak)
        │
        ▼
  Source closure ── single authoritative critic, hard-veto + witness chronology
        │
        ▼
  Rank ── transparent rubric composite + temporal-IoU suppression
        │
        ▼
  Export ── proof-checked promos.json / promos.csv
```

### Measured funnel (accepted run — from `outputs/promos.json` coverage)

| Stage | Count |
|---|---|
| Miner seeds (50 episodes mined, 0 failed windows) | 38 |
| Boundary options (12 openings rejected, 55 promising) | 67 |
| Planned semantic reviews | 33 |
| **Accepted promos** | **29** |
| Editorially rejected / never semantically reviewed / bounded-search untried | 4 / 34 / 22 |

## Engineering highlights

- **Atomic budget ledger** (`src/llm/budget.py`) — SQLite accounts/reservations/entries in micro-USD; unknown billing stays *reserved* (even across restarts), never optimistically released. Every model call is reserve → call → settle, with an explicit per-run cap and deadline.
- **Preflight before spending** (`src/llm/preflight.py`) — each model a profile can use is checked against the provider's live list, the price rule, and a real ping in the exact pipeline shape; failures return a readable gate before any real money is spent.
- **Role-chain model fallback** (`src/llm/router.py`) — generators fall back per call across a role's model chain; judge stages are *pinned* and re-run whole on the next model (never a mix of judges). Review is capped to ≤2 physical calls.
- **Failure taxonomy, not a crash** (`src/llm/router.py`, `src/llm/preflight.py`) — moderation → fallback (logged), persistent 429 → stage failover, capability errors → temperature/mode downgrade, truncation → larger token budget, schema errors → correction re-prompt.
- **Blind rendering + scrubbing** (`src/viewer.py`, `src/scrub.py`) — viewers/judges see clip-only evidence with anonymous speakers; names, relationships, spoiler terms and meta-words are stripped from non-dialogue (dialogue untouched), so titles/rationales can't leak into judgment.
- **Deterministic cut engine** (`src/cuts.py`, `src/cutstage.py`) — never cut inside a spoken line; start/end snapped to shot boundaries; any interval change invalidates derived claims.
- **Opening-first bounded review** (`src/finalcuts.py`) — identical openings are reviewed once (content-hash single-flight); a frozen, episode-ordered review set is fixed before dispatch, with coverage accounting that distinguishes accepted / editorially rejected / unreviewed / untried.
- **Source-closure verification** (`src/readiness.py`, `src/editorial.py`) — a single authoritative critic seat, full verdict re-validation, and witness-chronology checks (payoff/reopening) with hard vetoes (e.g. `PAYOFF_ALREADY_SHOWN`).
- **Transparent ranking** (`src/rubric.py`) — weighted composite **H 0.35 · T 0.30 · C 0.20 · P 0.15** (integer 1–5 inputs, validated), temporal-IoU dedup suppression at 0.60 with losers recorded as alternates, plus a `first_post` gate (H≥4 ∧ T≥4 ∧ min(C,P)≥3). No preference black box in the shipped path.
- **Price discipline** (`configs/models.yaml`) — every live model must be ≤ $0.50/M output tokens; unknown or over-rule prices are rejected at preflight. The full accepted run cost **$0.0178**.

## What you see

- **Results** — the accepted cuts: a clickable promo grid (hook, cliffhanger, exact source segments, rubric scores, honest limits), a 50-episode coverage map, and the full funnel above. Works offline; no API needed.
- **Try it yourself** — run the pipeline on your own screenplay ZIP with your own OpenRouter key (bring-your-own-key). The Results page never needs the API.

## Run it yourself (short)

Hosted, this is all a reviewer needs. To run locally or in the interview:

```text
# API (Python 3.12/3.13, venv outside this folder)
python -m pip install -r requirements.txt
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000 --workers 1

# Web UI (Node 22.12+)
npm --prefix frontend install
npm --prefix frontend run dev        # or: npm --prefix frontend run build
```

For a new screenplay pack: `python -m src parse --input "PATH"` then `python -m src run --input "PATH" --title "Show" --workers 4 --budget 0.25` (set `OPENROUTER_API_KEY` for the CLI). Give every new run a fresh run ID after changing input/code/prompts. Optional PDF/DOCX readers: `pip install pypdf==7.0.0`, `python-docx==1.2.0`.

## Hosting

- **Frontend** is a static Vite build deployed by `.github/workflows/pages.yml` (set `VITE_BASE=/<repo>/`; `VITE_API_BASE_URL` is an optional repo variable pointing at the API's `/api` origin — never put a provider key in `VITE_*`).
- **API** is FastAPI on Render via `render.yaml` (`PF_PRIVATE=0`, `PF_VISITOR_KEYS_ENABLED=1`, visitor-supplied keys). See `api/.env.example` for the backend environment.

Keep supplied screenplays and generated outputs private — they are submitted to the assignment separately, never committed here.
