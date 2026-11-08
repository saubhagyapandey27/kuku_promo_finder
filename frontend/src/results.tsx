import { useEffect, useMemo, useState } from "react";
import { type Showcase, type Card, clock, secs, comma } from "./showcase";

// Vite emits these as hashed assets and rewrites them for the configured base.
const AUDIO = [
  new URL("./assets/audio/top-1.mp3", import.meta.url).href,
  new URL("./assets/audio/top-2.mp3", import.meta.url).href,
  new URL("./assets/audio/top-3.mp3", import.meta.url).href,
  new URL("./assets/audio/top-4.mp3", import.meta.url).href,
  new URL("./assets/audio/top-5.mp3", import.meta.url).href,
];

const clamp2 = (s: string) => (s.length > 240 ? s.slice(0, 237) + "…" : s);

export function Results({ data }: { data: Showcase }) {
  const [selected, setSelected] = useState<Card | null>(null);
  const topIds = useMemo(() => new Set(data.top_five), [data]);
  const byId = useMemo(
    () => new Map(data.promos.map((p) => [p.promo_id, p])),
    [data],
  );
  const topCards = data.top_five
    .map((id) => byId.get(id))
    .filter((p): p is Card => !!p);

  const openByEpisode = (ep: number) => {
    const hit = data.promos.find((p) => p.episodes.includes(ep));
    if (hit) setSelected(hit);
  };

  return (
    <div className="results">
      <header className="hero">
        <span className="hero-eyebrow">Screenplay in, promos out</span>
        <h1>
          One show, one pass, <em>{data.stats.promos} clips</em> worth posting.
        </h1>
        <p className="hero-sub">
          A tool that reads timestamped microdrama screenplays and returns ranked
          20–60s promo moments — exact source timestamps, the hook, the
          cliffhanger, and one line on why it works. Built for an editor to check,
          not to replace.
        </p>
        <div className="stat-band">
          <Stat value={comma(data.stats.episodes)} label="Episodes read" />
          <Stat value={clock(data.stats.source_duration_s)} label="Screenplay runtime" />
          <Stat value={comma(data.stats.timed_blocks)} label="Timed blocks parsed" />
          <Stat value={comma(data.stats.promos)} label="Promos cut" accent />
          <Stat value={`${Math.round(data.stats.avg_promo_s)}s`} label="Average cut" />
        </div>
        <p className="hero-foot">
          Results got after using the tool on a given Kuku TV microdrama.
        </p>
      </header>

      <section className="section" id="coverage">
        <div className="section-head">
          <span className="eyebrow">EPISODE COVERAGE</span>
          <h2>Did we cover the show, and where can this go wrong?</h2>
          <p className="section-cap">
            Coverage and disposition are computed from the saved run. They are
            not human quality or conversion evidence.
          </p>
        </div>
        <div className="limits-grid">
          <div className="panel">
            <h3>Episode coverage</h3>
            <p className="fine">
              {data.coverage.represented_episodes.length} of{" "}
              {data.coverage.parsed_episodes.length} episodes have at least one
              exported cut. Click a filled box to read that promo.
            </p>
            <div className="coverage-grid" aria-label="Episode coverage">
              {data.coverage.parsed_episodes.map((ep) => {
                const on = data.coverage.represented_episodes.includes(ep);
                return (
                  <button
                    key={ep}
                    className={on ? "cov on" : "cov"}
                    aria-label={`Episode ${ep}${on ? " · open promo" : " · no exported cut"}`}
                    disabled={!on}
                    onClick={() => openByEpisode(ep)}
                  >
                    {ep}
                  </button>
                );
              })}
            </div>
            <p className="fine">
              Breadth is not recall: an episode with no exported cut does not
              mean no good promo exists. Exhaustive interval search:{" "}
              {data.coverage.exhaustive_interval_search ? "yes" : "no"}.
            </p>
          </div>

          <div className="panel">
            <h3>How 50 episodes became {data.stats.promos} promos</h3>
            <ul className="funnel">
              <li>
                <strong>{data.stats.seed_proposals}</strong> miner seeds across{" "}
                {data.coverage.parsed_episodes.length} mined episodes ({data.stats.mechanical_failures} mechanical seed failure)
              </li>
              <li>
                <strong>{data.stats.boundary_options}</strong> boundary options ({data.stats.rejected_opening} openings rejected)
              </li>
              <li>
                <strong>{data.stats.planned_reviews}</strong> planned reviews →{" "}
                <strong>{data.stats.accepted_reviews}</strong> accepted, {data.stats.editorial_rejections} editorially rejected
              </li>
              <li>
                <strong>{data.stats.unreviewed_options}</strong> candidates never semantically reviewed; {data.stats.bounded_search_untried} bounded-search options untried
              </li>
            </ul>
            <div className="funnel-bar" role="img" aria-label={`${data.stats.boundary_options} boundary options: ${data.stats.accepted_reviews} accepted, ${data.stats.editorial_rejections} editorially rejected, ${data.stats.bounded_search_untried} bounded-search untried, ${data.stats.rejected_opening} rejected openings`}>
              <span className="fb-accept" style={{ flex: data.stats.accepted_reviews }} />
              <span className="fb-rejected" style={{ flex: data.stats.editorial_rejections }} />
              <span className="fb-untried" style={{ flex: data.stats.bounded_search_untried }} />
              <span className="fb-opening" style={{ flex: data.stats.rejected_opening }} />
            </div>
            <ul className="funnel-legend">
              <li><i className="fb-swatch fb-accept" /> {data.stats.accepted_reviews} accepted → exported promos</li>
              <li><i className="fb-swatch fb-rejected" /> {data.stats.editorial_rejections} editorially rejected</li>
              <li><i className="fb-swatch fb-untried" /> {data.stats.bounded_search_untried} promising but bounded-search untried</li>
              <li><i className="fb-swatch fb-opening" /> {data.stats.rejected_opening} rejected openings</li>
            </ul>
            <p className="fine">
              A stricter rejection rate alone is not proof of a better product;
              good and bad cases were both inspected. Model agreement and passing
              checks are not human quality. Total exported runtime{" "}
              {clock(data.stats.total_promo_s)} across {data.stats.promos} cuts.
            </p>
          </div>
        </div>
      </section>

      <AudioSection cards={topCards} audio={AUDIO} onOpen={setSelected} />

      <section className="section" id="cuts">
        <div className="section-head">
          <span className="eyebrow">THE CUTS</span>
          <h2>Every promo, at a glance</h2>
          <p className="section-cap">
            {data.promos.length} ranked cuts. Click a card for the exact ordered
            timestamps, the full hook and cliffhanger, and where it can go wrong.
          </p>
        </div>
        <div className="promo-grid">
          {data.promos.map((p) => (
            <PromoCard
              key={p.promo_id}
              p={p}
              top={topIds.has(p.promo_id)}
              onOpen={() => setSelected(p)}
            />
          ))}
        </div>
      </section>

      <footer className="site-foot">
        <span>Microdrama Promo Finder · Saubhagya Pandey · Roll 230939</span>
      </footer>

      {selected && (
        <PromoDetail p={selected} top={topIds.has(selected.promo_id)} onClose={() => setSelected(null)} />
      )}
    </div>
  );
}

function Stat({ value, label, accent }: { value: string; label: string; accent?: boolean }) {
  return (
    <div className={accent ? "stat accent" : "stat"}>
      <span className="stat-value">{value}</span>
      <span className="stat-label">{label}</span>
    </div>
  );
}

function PromoCard({ p, top, onOpen }: { p: Card; top: boolean; onOpen: () => void }) {
  return (
    <article
      className={top ? "promo-card top" : "promo-card"}
      role="button"
      tabIndex={0}
      aria-label={`Open promo ${p.rank}: ${p.title}`}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen();
        }
      }}
    >
      <div className="card-top">
        <span className="card-rank">#{String(p.rank).padStart(2, "0")}</span>
        {top && <span className="card-flag">TOP 5</span>}
        <span className="card-ep">{p.episodes.map((e) => `E${e}`).join(" + ")}</span>
        <span className="card-dur">{Math.round(p.duration_s)}s</span>
      </div>
      <h3>{p.title}</h3>
      <div className="card-line">
        <span className="k">HOOK</span>
        <p>{clamp2(p.hook.quote || p.hook.description)}</p>
      </div>
      <div className="card-line">
        <span className="k">ENDS ON</span>
        <p>{clamp2(p.cliffhanger.open_question || p.cliffhanger.quote)}</p>
      </div>
      <div className="card-line">
        <span className="k">WHY</span>
        <p>{clamp2(p.why_it_works)}</p>
      </div>
      <span className="card-more">Read the cut →</span>
    </article>
  );
}

function AudioSection({
  cards,
  audio,
  onOpen,
}: {
  cards: Card[];
  audio: string[];
  onOpen: (p: Card) => void;
}) {
  return (
    <section className="section audio-section">
      <div className="section-head">
        <span className="eyebrow">AUDITION</span>
        <h2>Hear the five we'd post first</h2>
        <p className="section-cap">
          Each clip carries the real dialogue that opens the cut and the line it
          cuts on, performed as a short dramatic read. The words are the show's
          own; the delivery is a performance for feel only, not the final audio.
          Timing in these reads does not certify the 20–60s clip range.
        </p>
      </div>
      <div className="audio-list">
        {cards.map((p, i) => (
          <div className="audio-row" key={p.promo_id}>
            <div className="audio-rank">#{String(p.rank).padStart(2, "0")}</div>
            <div className="audio-info">
              <h3>{p.title}</h3>
              <p className="fine">{clamp2(p.hook.quote)}</p>
              <button className="text-btn" onClick={() => onOpen(p)}>
                Read this cut →
              </button>
            </div>
            <div className="audio-player">
              <audio controls preload="none" src={audio[i]} />
              <span className="fine">Synthetic read · {Math.round(p.duration_s)}s source cut</span>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function DetailBlock({
  label,
  quote,
  extra,
  at,
}: {
  label: string;
  quote: string;
  extra?: string;
  at?: string;
}) {
  return (
    <div className="detail-block">
      <span className="eyebrow">{label}</span>
      {quote && <blockquote>{quote}</blockquote>}
      {extra && <p className="fine">{extra}</p>}
      {at && <span className="ts">{at}</span>}
    </div>
  );
}

function PromoDetail({ p, top, onClose }: { p: Card; top: boolean; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [onClose]);

  const sc = p.scores;
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={`Promo ${p.rank}: ${p.title}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <span className="eyebrow">
            PROMO #{String(p.rank).padStart(2, "0")}{top ? " · TOP 5" : ""}
          </span>
          <button className="icon-btn" onClick={onClose} aria-label="Close">✕</button>
        </div>
        <h2>{p.title}</h2>
        <p className="modal-meta">
          {p.segments.map((s) => `E${s.episode} ${s.start}–${s.end}`).join("  ·  ")}
          {"  ·  "}
          {secs(p.duration_s)} · {p.structure}
        </p>

        <div className="detail-grid">
          <DetailBlock
            label="THE HOOK · FIRST 3 SECONDS"
            quote={p.hook.quote}
            extra={p.hook.description}
            at={p.hook.at}
          />
          <DetailBlock
            label="THE CLIFFHANGER"
            quote={p.cliffhanger.quote}
            extra={p.viewer_question ? `Open question: ${p.viewer_question}` : undefined}
            at={p.cliffhanger.at}
          />
        </div>

        <section className="detail-section">
          <span className="eyebrow">WHY IT WORKS</span>
          <p>{p.why_it_works}</p>
        </section>

        <section className="detail-section">
          <span className="eyebrow">ORDERED SOURCE SEGMENTS</span>
          <table className="seg-table">
            <thead>
              <tr><th>#</th><th>Episode</th><th>Start</th><th>End</th></tr>
            </thead>
            <tbody>
              {p.segments.map((s, i) => (
                <tr key={i}>
                  <td>{i + 1}</td>
                  <td>{s.episode}</td>
                  <td className="ts">{s.start}</td>
                  <td className="ts">{s.end}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="detail-section">
          <span className="eyebrow">RUBRIC · WEIGHTED COMPOSITE {String(sc.composite)}</span>
          <div className="score-row">
            {(["H", "T", "C", "P"] as const).map((k) => (
              <span className="score-chip" key={k}>
                <b>{k}</b> {sc[k]}
              </span>
            ))}
          </div>
          <p className="fine">
            H hook · T cliffhanger pull · C comprehension · P pacing.
          </p>
        </section>

        {p.witness && (
          <section className="detail-section">
            <span className="eyebrow">NEXT BEAT, OUTSIDE THIS CUT</span>
            <blockquote className="witness">{p.witness.quote}</blockquote>
            <p className="fine">
              The cut ends before this beat resolves — evidence the cliffhanger is
              genuinely open, not a paraphrase.
            </p>
          </section>
        )}

        <section className="detail-section">
          <span className="eyebrow">WHERE IT CAN GO WRONG</span>
          <p>{p.biggest_weakness}</p>
          {p.flags.length > 0 && (
            <p className="fine">Flags: {p.flags.join(" · ")}</p>
          )}
        </section>
      </div>
    </div>
  );
}
