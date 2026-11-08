import { useEffect, useRef, useState } from "react";
import { apiGet, apiPost, apiPostForm, loadConfig, loadRun } from "./api";
import { fromSaved, cutListCsv } from "./showcase";

// One-click action: the profile and spend cap are fixed server-side so the UI
// stays a single button. The API enforces an explicit positive budget; this is
// the cap the action uses. Gemini is optional (the pipeline falls back to
// OpenRouter models), so only an OpenRouter key is requested.
const PROFILE = "quality";
const FIXED_BUDGET = 3.0;

type Checked = { input_token: string; episodes: number; scenes: number; blocks: number };
type JobStatus = {
  state: string;
  phase: string;
  elapsed_s: number;
  cost_usd: number;
  count?: number;
  run_id?: string;
  error?: string;
};


const INSTRUCTIONS: string[] = [
  "Attach a ZIP containing one timestamped .txt screenplay file per episode.",
  "Name them in order: episode-001.txt, episode-002.txt, episode-003.txt, …",
  "Stamp every scene, line of dialogue and key action with HH:MM:SS.",
  "Plain text only — no PDFs, images or video needed.",
];
export function NewRun({ active }: { active: boolean }) {
  const [caps, setCaps] = useState<{ formats: string[]; visitor_enabled: boolean; visitor_reason: string }>();
  const [capsError, setCapsError] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [checked, setChecked] = useState<Checked>();
  const [key, setKey] = useState("");
  const [phase, setPhase] = useState<"idle" | "working" | "done" | "error">("idle");
  const [progress, setProgress] = useState(0);
  const [job, setJob] = useState(sessionStorage.getItem("promo-job") || "");
  const [status, setStatus] = useState<JobStatus>();
  const [message, setMessage] = useState("");
  const revision = useRef(0);

  useEffect(() => {
    if (!active) return;
    const ctrl = new AbortController();
    let stopped = false;
    let timer: number | undefined;
    // Free instances cold-start (up to ~1 min). Retry until the API answers
    // so one failed fetch can't brick the button for the whole session.
    const attempt = () => {
      loadConfig(ctrl.signal)
        .then((c) => {
          if (stopped) return;
          setCaps(c);
          setCapsError("");
        })
        .catch(() => {
          if (stopped) return;
          setCapsError("API is not reachable — retrying…");
          timer = setTimeout(attempt, 5000);
        });
    };
    attempt();
    return () => {
      stopped = true;
      ctrl.abort();
      clearTimeout(timer);
    };
  }, [active]);

  useEffect(() => {
    if (!job) return;
    const ctrl = new AbortController();
    const tick = async () => {
      try {
        const s = (await apiGet(`/jobs/${job}`, ctrl.signal)) as JobStatus;
        setStatus(s);
        setProgress(s.state === "done" ? 100 : Math.min(92, Math.round((s.elapsed_s / 300) * 100)));
        if (s.state === "done") {
          setPhase("done");
          setProgress(100);
          ctrl.abort();
        } else if (s.state === "error" || s.state === "interrupted") {
          setPhase("error");
          setMessage(s.error || "The run did not finish.");
          ctrl.abort();
        } else {
          setPhase("working");
          timer = setTimeout(tick, 2000);
        }
      } catch (e) {
        if ((e as Error).name !== "AbortError") {
          setPhase("error");
          setMessage((e as Error).message);
        }
      }
    };
    let timer = setTimeout(tick, 1200);
    return () => {
      ctrl.abort();
      clearTimeout(timer);
    };
  }, [job]);

  const change = (f: File[]) => {
    revision.current += 1;
    setFiles(f);
    setChecked(undefined);
    setPhase("idle");
    setMessage("");
  };


  const downloadCsv = async () => {
    if (!status?.run_id) return;
    try {
      const saved = await loadRun(status.run_id);
      const csv = cutListCsv(fromSaved(saved));
      const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
      const a = document.createElement("a");
      a.href = url;
      a.download = "promo-timestamps.csv";
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 4000);
    } catch (e) {
      setMessage((e as Error).message);
    }
  };

  const generate = async () => {
    const rev = ++revision.current;
    setPhase("working");
    setMessage("");
    setProgress(8);
    try {
      const form = new FormData();
      files.forEach((f) => form.append("files", f));
      const v = (await apiPostForm("/inputs/validate", form)) as Checked;
      if (rev !== revision.current) return;
      setChecked(v);
      setProgress(20);
      const started = (await apiPost("/jobs", {
        input_token: v.input_token,
        budget: FIXED_BUDGET,
        profile: PROFILE,
        openrouter_key: key.trim(),
      })) as { job_id: string };
      if (rev !== revision.current) return;
      sessionStorage.setItem("promo-job", started.job_id);
      setJob(started.job_id);
    } catch (e) {
      if (rev !== revision.current) return;
      setPhase("error");
      setMessage((e as Error).message);
    }
  };

  const running = phase === "working";
  const canGenerate = files.length > 0 && key.trim().length > 0 && !running && caps?.visitor_enabled !== false;

  return (
    <div className="newrun">
      <div className="section-head">
        <h2 className="tryit-title">
          Attach your screenplay, <em>Get the promo timestamps</em>
        </h2>
        <div className="instructions">
          <span className="instructions-head">INSTRUCTIONS for Zip</span>
          <ul className="instructions-list">
            {INSTRUCTIONS.map((line, i) => (
              <li key={i}>{line}</li>
            ))}
          </ul>
        </div>
      </div>

      <section className="tryit-panel">
        <div className="tryit-grid">
          <div className="tryit-col">
            <span className="eyebrow">1 · Your screenplay</span>
            <div className="run-actions">
              <label className="file-btn">
                Choose the ZIP (or files)
                <input
                  type="file"
                  multiple
                  accept={caps?.formats.map((x) => "." + x).join(",") || ".zip,.txt,.md"}
                  onChange={(e) => change(Array.from(e.target.files || []))}
                />
              </label>
            </div>
            {files.length > 0 && (
              <ul className="file-list">
                {files.map((f, i) => (
                  <li key={i} className="fine">
                    {f.name} · {(f.size / 1024).toFixed(1)} KB
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="tryit-col">
            <span className="eyebrow">2 · Your key (BYOK)</span>
            <p className="fine">
              Bring your own key. Only an <strong>OpenRouter</strong> key is needed —
              it runs this analysis and is discarded afterwards. Nothing is stored
              in your browser.
            </p>
            <label className="key-field">
              OpenRouter API key
              <input
                type="password"
                autoComplete="off"
                spellCheck={false}
                value={key}
                onChange={(e) => setKey(e.target.value)}
                disabled={running}
                placeholder="sk-or-…"
              />
            </label>
          </div>
        </div>

        <button className="btn-primary btn-big" disabled={!canGenerate} onClick={generate}>
          {running ? "Generating…" : "Generate promos"}
        </button>

        {!caps && (
          <p className="fine" role="status">
            {capsError || "Connecting to the server… (free instances wake slowly — up to ~1 minute)"}
          </p>
        )}
        {caps && !caps.visitor_enabled && (
          <p className="fine" role="status">
            {caps.visitor_reason}
          </p>
        )}

        {checked && phase !== "idle" && (
          <p className="run-facts" aria-live="polite">
            {checked.episodes} episodes · {checked.scenes} scenes · {checked.blocks} timed blocks
          </p>
        )}

        {running && (
          <div className="progress" aria-live="polite">
            <div className="progress-track">
              <div className="progress-fill" style={{ width: `${progress}%` }} />
            </div>
            <p className="fine">
              {status?.phase || "Finding promo cuts"} · about {progress}% · {status?.elapsed_s ?? 0}s
            </p>
          </div>
        )}

        {phase === "done" && status?.run_id && (
          <div className="tryit-done" aria-live="polite">
            <p className="run-facts">
              Done — {status.count} promos cut in {status.elapsed_s}s.
            </p>
            <button className="btn-primary" onClick={downloadCsv}>
              ↓ Download promo timestamps CSV
            </button>
          </div>
        )}

        {phase === "error" && <p className="notice" role="status">{message}</p>}
        {message && phase !== "error" && <p className="notice" role="status">{message}</p>}
      </section>
    </div>
  );
}
