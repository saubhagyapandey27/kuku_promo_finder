// Types and helpers shared by the single-page results view and the API boundary.

export type Segment = {
  episode: number;
  start: string;
  end: string;
  start_s: number;
  end_s: number;
};

export type Event = {
  block_id: string;
  episode: number;
  kind: string;
  speaker: string;
  start_s: number;
  end_s: number;
  text: string;
  timing_basis?: string;
  uncertainty?: unknown;
};

export type Assessment = {
  trigger_event?: string;
  target?: string;
  visible_stake?: string;
  viewer_question?: string;
  question_kind?: string;
  resolved_in_clip?: boolean;
  ending_summary?: string;
  why_it_works?: string;
  biggest_weakness?: string;
};

export type Witness = { block_id?: string; quote: string };

export type Promo = {
  promo_id: string;
  rank: number;
  title?: string;
  structure?: string;
  segments: Segment[];
  duration_s?: number;
  selected_transcript?: Event[];
  hook?: { quote?: string; at?: string; description?: string };
  cliffhanger?: { quote?: string; at?: string; open_question?: string };
  eligibility?: {
    assessment?: Assessment;
    ranking?: { inputs?: Record<string, number>; score?: number };
    readiness?: { reviews?: { postcut_response?: Witness[] }[] };
  };
  why_it_works?: string;
  biggest_weakness?: string;
  scores?: Record<string, number>;
  flags?: string[];
  postcut?: Witness[];
  first_post?: boolean;
};

export type Coverage = {
  parsed_episodes?: number[];
  episodes_in_final_cuts?: number[];
  planned_semantic_count?: number;
  planned_semantic_states?: Record<string, number>;
  complete_coverage?: boolean;
  exhaustive_interval_search?: boolean;
  seed_proposals?: number;
  boundary_options?: number;
  opening_states?: { rejected_opening?: number };
  semantically_unreviewed_options?: number;
  bounded_search_untried?: number;
  mechanical_seed_failures?: unknown[];
};

export type Saved = {
  show?: string;
  note?: string;
  promos: Promo[];
  posting_plan?: {
    posting_order?: string[];
    first_post_shortlist?: string[];
    top_five?: string[];
  };
  coverage?: Coverage;
  cost?: { total_usd?: number; cache_hits?: number; budget_usd?: number };
  sha256?: string;
  ranking_method?: {
    mode?: string;
    weights?: Record<string, number>;
    iou_threshold?: number;
  };
};

export const missing = "Not recorded in this saved run";
export const title = (p: Promo) => p.title || `Promo ${p.rank}`;
export const duration = (p: Promo) =>
  p.segments.reduce((n, s) => n + (s.end_s - s.start_s), 0);

export function stamp(s: number): string {
  if (!isFinite(s)) s = 0;
  let cs = Math.round(Math.max(0, s) * 100);
  const m = Math.floor(cs / 6000);
  const sec = (cs - m * 6000) / 100;
  return `${String(m).padStart(2, "0")}:${sec.toFixed(2).padStart(5, "0")}`;
}

export function timing(basis: string): string {
  return (
    {
      "source-event-complete": "Complete source event",
      "word-prefix-estimated": "Word prefix estimated; no audio alignment",
      "partial-visual-uncertain": "Partial visual; action timing uncertain",
    } as Record<string, string>
  )[basis] || basis;
}

export function order(run: Saved): Promo[] {
  const map = new Map(run.promos.map((p) => [p.promo_id, p]));
  const ids = run.posting_plan?.posting_order;
  if (!ids) return run.promos;
  const saved = ids.flatMap((id) => (map.has(id) ? [map.get(id)!] : []));
  return [...saved, ...run.promos.filter((p) => !ids.includes(p.promo_id))];
}

export const assessmentOf = (p: Promo): Assessment =>
  p.eligibility?.assessment || {};

export const viewerQuestion = (p: Promo): string =>
  assessmentOf(p).viewer_question || p.cliffhanger?.open_question || missing;

export const rationale = (p: Promo): string =>
  assessmentOf(p).why_it_works || p.why_it_works || missing;

export const weakness = (p: Promo): string =>
  assessmentOf(p).biggest_weakness || p.biggest_weakness || missing;

export function scores(p: Promo): Record<string, number> {
  const r = p.eligibility?.ranking?.inputs;
  if (r) return r;
  const s = p.scores || {};
  return {
    H: s.hook ?? 0,
    T: s.cliff_pull ?? 0,
    C: s.comprehension ?? 0,
    P: s.pacing ?? 0,
  };
}

export const aggregate = (p: Promo): number | string =>
  p.eligibility?.ranking?.score ?? p.scores?.composite ?? missing;

export function postcutOf(p: Promo): Witness[] {
  if (p.postcut?.length) return p.postcut;
  const reviews = p.eligibility?.readiness?.reviews || [];
  const out: Witness[] = [];
  for (const r of reviews)
    for (const w of r.postcut_response || [])
      out.push({ block_id: w.block_id, quote: w.quote });
  return out;
}
