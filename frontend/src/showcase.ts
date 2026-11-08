// Compact, public-safe projection of the accepted run (generated once from the
// accepted outputs; see scripts/build_showcase.py in the working area).
// Full transcripts, opening packets and source screenplays are NOT included.

import bundled from "./showcase.json";
import {
  type Saved,
  type Promo,
  order,
  scores,
  rationale,
  weakness,
  viewerQuestion,
  aggregate,
  postcutOf,
} from "./model";

export type Seg = { episode: number; start: string; end: string };
export type ScoreSet = {
  H: number;
  T: number;
  C: number;
  P: number;
  composite: number | string;
};
export type Card = {
  rank: number;
  promo_id: string;
  title: string;
  duration_s: number;
  structure: string;
  segments: Seg[];
  episodes: number[];
  hook: { quote: string; at: string; description: string };
  cliffhanger: { quote: string; open_question: string; at: string };
  why_it_works: string;
  biggest_weakness: string;
  viewer_question: string;
  ending_summary: string;
  scores: ScoreSet;
  flags: string[];
  witness: { block_id: string; quote: string } | null;
  first_post: boolean;
};

export type Stats = {
  episodes: number;
  source_duration_s: number;
  timed_blocks: number;
  script_lines: number;
  promos: number;
  avg_promo_s: number;
  total_promo_s: number;
  cost_usd: number;
  planned_reviews: number;
  editorial_rejections: number;
  accepted_reviews: number;
  ambiguous_review: number;
  operational_failure: number;
  budget_deferral: number;
  seed_proposals: number;
  boundary_options: number;
  rejected_opening: number;
  unreviewed_options: number;
  bounded_search_untried: number;
  mechanical_failures: number;
};

export type Coverage = {
  parsed_episodes: number[];
  represented_episodes: number[];
  complete_coverage: boolean;
  exhaustive_interval_search: boolean;
};

export type Showcase = {
  kind: string;
  source_note: string;
  show: string;
  note: string;
  stats: Stats;
  coverage: Coverage;
  ranking_weights: Record<string, number>;
  top_five: string[];
  promos: Card[];
};

export const SHOWCASE = bundled as unknown as Showcase;

export const clock = (s: number): string => {
  const h = Math.floor(s / 3600);
  return h > 0
    ? `${h}h ${String(Math.round((s % 3600) / 60)).padStart(2, "0")}m`
    : `${Math.round(s / 60)}m`;
};
export const secs = (s: number): string => `${s.toFixed(1)}s`;
export const money = (n: number): string => `$${n.toFixed(n < 0.1 ? 4 : 3)}`;
export const comma = (n: number): string => n.toLocaleString("en-US");
/** Map an API-loaded run into the same display shape as the bundled showcase. */
export function fromSaved(saved: Saved): Showcase {
  const list = order(saved);
  const episodes = new Set<number>();
  let total = 0;
  const promos: Card[] = list.map((p: Promo, i: number) => {
    const d = p.duration_s ?? 0;
    total += d;
    p.segments.forEach((s) => episodes.add(s.episode));
    const sc = scores(p);
    const w = postcutOf(p)[0];
    return {
      rank: p.rank ?? i + 1,
      promo_id: p.promo_id,
      title: p.title || `Promo ${p.rank ?? i + 1}`,
      duration_s: d,
      structure: p.structure || "continuous",
      segments: p.segments.map((s) => ({
        episode: s.episode,
        start: s.start,
        end: s.end,
      })),
      episodes: sortedUnique(p.segments.map((s) => s.episode)),
      hook: {
        quote: p.hook?.quote || "",
        at: p.hook?.at || "",
        description: p.hook?.description || "",
      },
      cliffhanger: {
        quote: p.cliffhanger?.quote || "",
        open_question: p.cliffhanger?.open_question || "",
        at: p.cliffhanger?.at || "",
      },
      why_it_works: rationale(p),
      biggest_weakness: weakness(p),
      viewer_question: viewerQuestion(p),
      ending_summary: p.eligibility?.assessment?.ending_summary || "",
      scores: {
        H: sc.H ?? 0,
        T: sc.T ?? 0,
        C: sc.C ?? 0,
        P: sc.P ?? 0,
        composite: aggregate(p),
      },
      flags: p.flags || [],
      witness: w ? { block_id: w.block_id || "", quote: w.quote } : null,
      first_post: !!p.first_post,
    };
  });
  const n = promos.length || 1;
  const parsed = saved.coverage?.parsed_episodes || [];
  const representative = sortedUnique([...episodes]);
  return {
    kind: "live_run",
    source_note: "Loaded from the connected API for this run.",
    show: saved.show || "Uploaded screenplay",
    note: saved.note || "",
    stats: {
      episodes: parsed.length || representative.length,
      source_duration_s: 0,
      timed_blocks: 0,
      script_lines: 0,
      promos: promos.length,
      avg_promo_s: total / n,
      total_promo_s: total,
      cost_usd: saved.cost?.total_usd || 0,
      planned_reviews: saved.coverage?.planned_semantic_count || promos.length,
      editorial_rejections:
        saved.coverage?.planned_semantic_states?.editorially_rejected || 0,
      accepted_reviews: promos.length,
      ambiguous_review: 0,
      operational_failure: 0,
      budget_deferral: 0,
      seed_proposals: saved.coverage?.seed_proposals || 0,
      boundary_options: saved.coverage?.boundary_options || 0,
      rejected_opening: saved.coverage?.opening_states?.rejected_opening || 0,
      unreviewed_options: saved.coverage?.semantically_unreviewed_options || 0,
      bounded_search_untried: saved.coverage?.bounded_search_untried || 0,
      mechanical_failures: saved.coverage?.mechanical_seed_failures?.length || 0,
    },
    coverage: {
      parsed_episodes: parsed,
      represented_episodes: representative,
      complete_coverage: !!saved.coverage?.complete_coverage,
      exhaustive_interval_search: false,
    },
    ranking_weights: saved.ranking_method?.weights || {},
    top_five: saved.posting_plan?.top_five || promos.slice(0, 5).map((p) => p.promo_id),
    promos,
  };
}

function sortedUnique(xs: number[]): number[] {
  return [...new Set(xs)].sort((a, b) => a - b);
}

/** Client-side editor cut list: ordered segments, hook, cliffhanger, rationale. */
export function cutListCsv(s: Showcase): string {
  const esc = (v: unknown) => {
    const str = String(v ?? "");
    return /[",\n]/.test(str) ? `"${str.replace(/"/g, '""')}"` : str;
  };
  const rows = [["rank", "promo_id", "title", "segment_order", "episode", "start", "end", "hook", "cliffhanger", "why_it_works"]];
  for (const p of s.promos)
    p.segments.forEach((g, i) =>
      rows.push([
        p.rank, p.promo_id, p.title, i + 1, g.episode, g.start, g.end,
        p.hook.quote, p.cliffhanger.quote, p.why_it_works,
      ].map(esc) as unknown as string[]),
    );
  return rows.map((r) => r.join(",")).join("\r\n") + "\r\n";
}

export function downloadCsv(s: Showcase): void {
  const blob = new Blob([cutListCsv(s)], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "editor_cut_list.csv";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
