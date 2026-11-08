# Microdrama Promo Finder

Built by **Saubhagya Pandey (Roll No. 230939)** for Problem 1 of the Kuku AI Generalist Assignment. It reads timestamped microdrama screenplays and returns ranked 20–60 second promo cuts — exact ordered episode timestamps, the opening hook, the cliffhanger, and one line on why each works. The accepted result on the bundled show is 29 promos from 50 episodes.

**Live site (GitHub Pages):** https://saubhagyapandey27.github.io/kuku_promo_finder/

## What you see

- **Results** — the accepted cuts for the bundled show: a clickable promo grid (hook, cliffhanger, exact source segments, rubric scores, honest limits), a 50-episode coverage map, and the full funnel (38 miner seeds → 67 boundary options → 33 reviews → 29 promos). Works offline; no API needed.
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

For a new screenplay pack: pass its path on the command line, e.g. `python -m src parse --input "PATH"` then `python -m src run --input "PATH" --title "Show" --workers 4 --budget 0.25` (set `OPENROUTER_API_KEY` for the CLI). Give every new run a fresh run ID after changing input/code/prompts. Optional PDF/DOCX readers: `pip install pypdf==7.0.0`, `python-docx==1.2.0`.

## Hosting

- **Frontend** is a static Vite build deployed by `.github/workflows/pages.yml` (set `VITE_BASE=/<repo>/`; `VITE_API_BASE_URL` is an optional repo variable pointing at the API's `/api` origin — never put a provider key in `VITE_*`).
- **API** is FastAPI on Render via `render.yaml` (`PF_PRIVATE=0`, `PF_VISITOR_KEYS_ENABLED=1`, visitor-supplied keys). See `api/.env.example` for the backend environment.

Keep supplied screenplays and generated outputs private — they are submitted to the assignment separately, never committed here.
