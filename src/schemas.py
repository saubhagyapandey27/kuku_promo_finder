"""Core data models (parsed screenplay) shared by every stage.

LLM-facing schemas (bible, miner, judges...) are added to this module as the stages are built.
"""
from __future__ import annotations

from typing import List, Optional
from enum import Enum

from pydantic import BaseModel, Field, model_validator

KINDS = ("SHOT", "ACTION", "SFX", "LINE")


class Block(BaseModel):
    id: str                      # E03-B045
    ep: int
    idx: int                     # 1-based order within the episode
    kind: str                    # SHOT | ACTION | SFX | LINE
    label: str = ""              # raw label, e.g. "BLOND NOBLEMAN (Shocked, Trembling)"
    speaker: str = ""            # LINE only: label without parentheticals, e.g. "BLOND NOBLEMAN"
    delivery: str = ""           # LINE only: delivery tags, "Shocked, Trembling"
    text: str = ""
    t0: float
    t1: float
    scene: int = 0               # scene index within the episode (0-based)

    @property
    def dur(self) -> float:
        return max(0.0, self.t1 - self.t0)


class Scene(BaseModel):
    ep: int
    index: int
    heading: str = ""
    start: float = 0.0
    end: float = 0.0


class Episode(BaseModel):
    ep: int
    logline: str = ""
    continuity: str = ""
    scenes: List[Scene] = Field(default_factory=list)
    blocks: List[Block] = Field(default_factory=list)
    duration: float = 0.0        # last block end / last scene end
    source: str = ""             # file name


class Show(BaseModel):
    title: str = ""
    episodes: List[Episode] = Field(default_factory=list)

    def episode(self, n: int) -> Optional[Episode]:
        for e in self.episodes:
            if e.ep == n:
                return e
        return None

    def block_index(self) -> dict:
        return {b.id: b for e in self.episodes for b in e.blocks}


class ParseIssue(BaseModel):
    ep: Optional[int] = None
    level: str = "warn"          # warn | error
    msg: str
    line: Optional[int] = None


class ParseReport(BaseModel):
    episodes: int = 0
    blocks: int = 0
    lines: int = 0
    issues: List[ParseIssue] = Field(default_factory=list)
    per_episode: List[dict] = Field(default_factory=list)

    @property
    def errors(self) -> List[ParseIssue]:
        return [i for i in self.issues if i.level == "error"]


# ----- time helpers -------------------------------------------------------------------------

def fmt_t(sec: float) -> str:
    """Seconds -> 'MM:SS.cc' (episode-relative, same style as the screenplays)."""
    if sec < 0:
        sec = 0.0
    total_cs = int(round(sec * 100))
    m, rem = divmod(total_cs, 6000)
    s, cs = divmod(rem, 100)
    return f"{m:02d}:{s:02d}.{cs:02d}"


# =============================================================================================
# LLM-facing schemas (plan §6). Required = what code needs; descriptive fields default so a model
# that omits one doesn't throw away an otherwise valid candidate.
# =============================================================================================
from typing import Literal  # noqa: E402

HookType = Literal["violence", "humiliation", "shocking_line", "reveal", "threat_ultimatum", "accusation",
                   "defiance_powerflip", "romance_tension", "mystery_question", "danger_spectacle", "other"]
CliffType = Literal["pending_decision", "impending_harm", "unanswered_question", "interrupted_reveal",
                    "arrival_intrusion", "threat_issued", "payoff_denied", "other"]


# ---- P1 bible -----------------------------------------------------------------------------
class VisualTag(BaseModel):
    from_episode: int
    to_episode: int
    tag: str


class AcquisitionAngle(BaseModel):
    name: str
    promise: str = ""
    tropes: List[str] = Field(default_factory=list)


class Character(BaseModel):
    name: str
    aliases: List[str] = Field(default_factory=list)       # other names/titles used in the text ("Lady Juliana")
    role: str = ""
    description: str = ""
    relationships: List[str] = Field(default_factory=list)
    visual_tags: List[VisualTag] = Field(default_factory=list)
    secret: Optional[str] = None
    secret_revealed_ep: Optional[int] = None


class SpeakerAlias(BaseModel):
    label: str
    character: str
    episodes: List[int] = Field(default_factory=list)       # empty = all episodes the label appears in
    confidence: float = 0.8


class EpisodeArc(BaseModel):
    episode: int
    summary: str = ""
    ends_on: str = ""
    needs_prior_context: bool = False


class Reveal(BaseModel):
    episode: int
    reveal: str


class SensitiveNote(BaseModel):
    episode: int
    note: str


class SeriesBible(BaseModel):
    premise: str
    tropes: List[str] = Field(default_factory=list)
    acquisition_angles: List[AcquisitionAngle] = Field(default_factory=list)
    characters: List[Character] = Field(default_factory=list)
    speaker_aliases: List[SpeakerAlias] = Field(default_factory=list)
    spoiler_terms: List[str] = Field(default_factory=list)
    episode_arc: List[EpisodeArc] = Field(default_factory=list)
    reveals: List[Reveal] = Field(default_factory=list)
    sensitive_content: List[SensitiveNote] = Field(default_factory=list)
    continuity_issues: List[str] = Field(default_factory=list)


# ---- P3 miner / P4 composer / P5 repair -------------------------------------------------------
class SegmentPick(BaseModel):
    start_block: str
    end_block: str
    purpose: str = ""


class Candidate(BaseModel):
    episode_of_hook: int = 0
    label: str = ""
    rationale: str = ""
    segments: List[SegmentPick]
    hook_block: str
    cliff_block: str
    peak_block: str = ""
    hook_quote: str = ""
    cliff_quote: str = ""
    hook_type: HookType = "other"
    cliff_type: CliffType = "other"
    hook_description: str = ""
    cliff_description: str = ""
    conflict_thread: str = ""
    open_question: str = ""
    missing_context: List[str] = Field(default_factory=list)
    est_duration_s: float = 0.0
    confidence: float = 0.5
    required_setup_ids: List[str] = Field(default_factory=list, max_length=6)


class MinerOutput(BaseModel):
    candidates: List[Candidate]


class ComposerPromo(BaseModel):
    label: str = ""
    rationale: str = ""
    segments: List[SegmentPick]
    hook_block: str
    cliff_block: str
    peak_block: str = ""
    hook_quote: str = ""
    cliff_quote: str = ""
    hook_type: HookType = "other"
    cliff_type: CliffType = "other"
    link: str = ""
    conflict_thread: str = ""
    open_question: str = ""
    continuity_risk: Optional[str] = None


class ComposerOutput(BaseModel):
    promos: List[ComposerPromo]


class RepairOutput(BaseModel):
    reasoning: str = ""
    give_up: bool = False
    segments: List[SegmentPick] = Field(default_factory=list)
    hook_block: str = ""
    cliff_block: str = ""


# ---- P7a / P7b / P7c / P8 -----------------------------------------------------------------
class HookItem(BaseModel):
    id: str
    gut_reaction: str = ""
    question_raised: Optional[str] = None
    stop_power: int = Field(ge=1, le=5)
    cue_ids: List[str] = Field(min_length=1)
    cue_summary: str = Field(min_length=1)
    timing_uncertainty: str
    critical_uncertainty: bool


class HookBatch(BaseModel):
    items: List[HookItem]


class ColdPerson(BaseModel):
    description: str = ""
    relationship: str = ""


class ColdViewer(BaseModel):
    story: str
    people: List[ColdPerson] = Field(default_factory=list)
    relationship_clear_at_s: Optional[float] = None
    confusion: List[str] = Field(default_factory=list)
    final_moment: str = ""
    want_to_see_next: str = ""
    anticipation: Literal["uncertainty", "payoff", "none"] = "none"
    same_conflict: bool = True
    resolved_inside: bool = False
    resolved_reason: str = ""
    cliff_pull: int = Field(ge=1, le=5)
    tap_to_continue: int = Field(ge=1, le=5)


class TerminalAssessment(BaseModel):
    """Versioned final-cut contract; runtime integration follows in the semantic eligibility stage.

    Evidence membership and timing still require source validation; this only rejects internally
    contradictory approvals. No field defaults to a favorable assessment.
    """
    pending_consequence: str
    state: Literal['pending', 'paid_off', 'causally_reopened', 'unclear']
    same_conflict: bool
    resolved_inside: bool
    setup_evidence_ids: List[str]
    terminal_evidence_ids: List[str]
    payoff_evidence_ids: List[str]
    reopening_evidence_ids: List[str]
    missing_setup_ids: List[str]
    material_timing_unknown: bool
    verdict: Literal['eligible', 'ineligible', 'uncertain']
    reason: str = Field(min_length=1)

    @model_validator(mode='after')
    def reject_contradictory_approval(self):
        if self.verdict == 'eligible':
            if (self.state not in ('pending', 'causally_reopened') or not self.pending_consequence.strip()
                    or not self.setup_evidence_ids or not self.terminal_evidence_ids
                    or self.missing_setup_ids or self.material_timing_unknown or not self.same_conflict):
                raise ValueError('eligibility requires a supported pending beat and complete causal setup')
            if self.resolved_inside and self.state != 'causally_reopened':
                raise ValueError('paid-off central conflict cannot be approved')
            if self.state == 'causally_reopened' and not self.reopening_evidence_ids:
                raise ValueError('a reopened conflict requires linking evidence')
        return self


class EditorialIssue(BaseModel):
    category: Literal['setup','closure','consequence','identity','continuity','timing','hook']
    severity: Literal['essential','optional']
    evidence_ids: List[str]
    detail: str = Field(min_length=1)
    repair: str = Field(min_length=1)


class ConsequenceRecord(BaseModel):
    action: str = Field(min_length=1)
    target: str = Field(min_length=1)
    valued_stake: str = Field(min_length=1)
    action_ids: List[str] = Field(min_length=1)
    target_ids: List[str] = Field(min_length=1)
    stake_ids: List[str] = Field(min_length=1)

    @model_validator(mode='after')
    def nonblank_claims(self):
        if any(not s.strip() for s in (self.action,self.target,self.valued_stake)):
            raise ValueError('source consequence claims must be nonblank')
        return self


class ContentEvidence(BaseModel):
    block_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)

    @model_validator(mode='after')
    def nonblank(self):
        if not self.block_id.strip() or not self.quote.strip():
            raise ValueError('evidence ID and content quote must be nonblank')
        return self


class FinalViewer(BaseModel):
    """Current blind selected-cut contract. Historical records remain readable as Promo dictionaries."""
    model_config = {'extra':'forbid'}
    trigger_event: str = Field(min_length=1)
    target: str = Field(min_length=1)
    visible_stake: str = Field(min_length=1)
    viewer_question: str = Field(min_length=1)
    question_kind: Literal['danger','choice','identity_or_motive','interruption','confrontation','ordinary_reaction','none']
    resolved_in_clip: bool
    setup_quote_ids: List[str] = Field(min_length=1)
    terminal_quote_ids: List[str] = Field(min_length=1)
    evidence: List[ContentEvidence] = Field(min_length=1, max_length=12)
    cliff: int = Field(ge=1,le=5)
    clarity: int = Field(ge=1,le=5)
    pacing: int = Field(ge=1,le=5)
    title: str = Field(min_length=1)
    ending_summary: str = Field(min_length=1)
    why_it_works: str = Field(min_length=1)
    biggest_weakness: str = Field(min_length=1)
    critical_uncertainty: bool

    @model_validator(mode='after')
    def nonblank_claims(self):
        for key in ('trigger_event','target','visible_stake','viewer_question','title','ending_summary','why_it_works','biggest_weakness'):
            if not getattr(self,key).strip():
                raise ValueError(key+' must be nonblank')
        return self


class BoundaryEnding(BaseModel):
    id: str = ''
    end_block: str
    question: str
    state: Literal['pending', 'paid_off', 'causally_reopened', 'unclear']
    required_ids: List[str]
    payoff_ids: List[str]
    reopening_ids: List[str]
    consequence: ConsequenceRecord


class BoundaryTemplate(BaseModel):
    id: str
    hook_block: str
    segments: List[SegmentPick] = Field(min_length=1, max_length=3)
    ending_ids: List[str]
    added_beat_ids: List[str]
    label: str


class BoundaryAudit(BaseModel):
    reasoning: str
    cannot_support: bool
    endings: List[BoundaryEnding] = Field(max_length=3)
    templates: List[BoundaryTemplate] = Field(max_length=2)
    unknowns: List[str]

    @model_validator(mode='before')
    @classmethod
    def bound_search_width(cls, value):
        # Excess alternatives are safely discarded, never semantically reinterpreted.
        # A provider ignoring maxItems should not lose every otherwise valid option.
        if isinstance(value, dict):
            value = dict(value)
            capped = False
            for field, limit in (('endings', 3), ('templates', 2)):
                if isinstance(value.get(field), list) and len(value[field]) > limit:
                    value[field] = value[field][:limit]
                    capped = True
            if capped:
                value['unknowns'] = list(value.get('unknowns') or []) + ['Excess alternatives discarded at deterministic search-width cap.']
        return value


class BackwardSelection(BaseModel):
    """Choose only precomputed timing options; never author the verified consequence."""
    option_ids: List[str] = Field(max_length=2)
    reason: str = Field(min_length=1)


class ProducerItem(BaseModel):
    id: str
    comprehension_errors: List[str] = Field(default_factory=list)
    comprehension_score: int = Field(ge=1, le=5)
    thread_ok: bool = True
    ua_angle: str = ""
    trope_appeal: int = Field(ge=1, le=5, default=3)
    emotional_intensity: int = Field(ge=1, le=5, default=3)
    platform_risk: Literal["none", "low", "high"] = "none"
    risk_reason: str = ""
    spoiler_level: Literal["none", "premise", "major"] = "none"
    why_it_works: str = ""
    biggest_weakness: str = ""
    suggested_caption: Optional[str] = None


class ProducerBatch(BaseModel):
    items: List[ProducerItem]


class ListwiseNote(BaseModel):
    id: str
    note: str = ""


class ListwiseRanking(BaseModel):
    notes: List[ListwiseNote] = Field(default_factory=list)
    ranking: List[str]


class FinalReview(BaseModel):
    id: str
    verdict: Literal['pass','withhold','uncertain']
    evidence_ids: List[str] = Field(min_length=1)
    reason: str = Field(min_length=1)


class FinalRanking(ListwiseRanking):
    reviews: List[FinalReview]


class ReadinessEvidence(ContentEvidence):
    pass


class ClaimSupport(BaseModel):
    judgment: Literal['supported','unsupported','uncertain']
    evidence: List[ReadinessEvidence]
    reason: str = Field(min_length=1)

    @model_validator(mode='after')
    def requires_evidence(self):
        if self.judgment=='supported' and not self.evidence:
            raise ValueError('a supported claim needs its own evidence')
        return self


class PartialCheck(BaseModel):
    block_id: str
    judgment: Literal['harmless','possible_payoff','uncertain']
    reason: str = Field(min_length=1)


class ObjectionCode(str,Enum):
    PAYOFF_ALREADY_SHOWN='PAYOFF_ALREADY_SHOWN'
    EMOTIONAL_STAKES_SETTLED='EMOTIONAL_STAKES_SETTLED'
    NO_PENDING_ACTION_AT_CUT='NO_PENDING_ACTION_AT_CUT'
    HALLUCINATED_CORE_CONFLICT='HALLUCINATED_CORE_CONFLICT'
    MISSING_CAUSAL_TRIGGER_IN_CLIP='MISSING_CAUSAL_TRIGGER_IN_CLIP'
    INCOHERENT_SEGMENT_JUMP='INCOHERENT_SEGMENT_JUMP'
    UNANCHORED_PARTIAL_SHOT_SPOILER='UNANCHORED_PARTIAL_SHOT_SPOILER'
    UNSTATED_CHARACTER_NAME='UNSTATED_CHARACTER_NAME'
    UNSTATED_TITLE_OR_AUTHORITY='UNSTATED_TITLE_OR_AUTHORITY'
    UNSTATED_PRIOR_BACKSTORY='UNSTATED_PRIOR_BACKSTORY'
    MINOR_MOTIVATION_AMBIGUITY='MINOR_MOTIVATION_AMBIGUITY'


class ClosureIssue(BaseModel):
    code: ObjectionCode
    evidence: List[ReadinessEvidence] = Field(default_factory=list)
    reason: str = ''

    @model_validator(mode='before')
    @classmethod
    def notes_are_not_proof_obligations(cls,value):
        from .editorial import NON_VETO
        if isinstance(value,dict) and value.get('code') in NON_VETO:
            value=dict(value)
            value['evidence']=[e for e in value.get('evidence',[]) if isinstance(e,dict)
                and isinstance(e.get('block_id'),str) and e['block_id'].strip()
                and isinstance(e.get('quote'),str) and e['quote'].strip()]
        return value

    @model_validator(mode='after')
    def hard_issue_needs_evidence(self):
        from .editorial import HARD_VETO
        if self.code.value in HARD_VETO and (not self.evidence or not self.reason.strip()):
            raise ValueError('HARD_VETO needs nonblank content evidence and a reason')
        return self


class ReadinessItem(BaseModel):
    id: str
    # Evidence precedes conclusions. These are witnesses for the SAME two questions,
    # never a new intended consequence or an additional objection referee.
    source_clarification: List[ReadinessEvidence] = Field(max_length=1)
    in_clip_payoff: List[ReadinessEvidence] = Field(max_length=1)
    reopening_trigger: List[ReadinessEvidence] = Field(max_length=1)
    postcut_response: List[ReadinessEvidence] = Field(max_length=1)
    evidence: List[ReadinessEvidence] = Field(min_length=1)
    issues: List[ClosureIssue]
    core_conflict_grounded: bool
    payoff_withheld_at_cut: bool
    reason: str = Field(min_length=1)

    @model_validator(mode='after')
    def closure_flags_must_agree(self):
        closure={'PAYOFF_ALREADY_SHOWN','EMOTIONAL_STAKES_SETTLED'}
        if any(i.code.value in closure for i in self.issues):
            if self.payoff_withheld_at_cut:
                raise ValueError('closure veto contradicts payoff_withheld_at_cut=true; report a consistent judgment, not both')
            if not self.in_clip_payoff:
                raise ValueError('closure veto requires an exact selected in_clip_payoff answer witness')
        return self

    @property
    def verdict(self):
        from .editorial import critic_approves
        return 'ready' if critic_approves(self) else 'withhold'

    @property
    def hard_veto_issues(self):
        from .editorial import HARD_VETO
        return [i for i in self.issues if i.code.value in HARD_VETO]


class ReadinessBatch(BaseModel):
    items: List[ReadinessItem]


# =============================================================================================
# Pipeline record: one promo candidate as it flows S3 -> S9
# =============================================================================================
from typing import Any, Dict, Tuple  # noqa: E402


class Promo(BaseModel):
    id: str
    source: str = "miner"                       # miner | composer | flash_forward | b1 | b3
    window: int = 0
    label: str = ""
    rationale: str = ""
    # what was requested (after any repair): block-ID pairs per segment, play order
    pick: List[Tuple[str, str]] = Field(default_factory=list)
    hook_block: str = ""
    cliff_block: str = ""
    peak_block: str = ""
    hook_quote: str = ""
    cliff_quote: str = ""
    hook_type: str = "other"
    cliff_type: str = "other"
    hook_description: str = ""
    cliff_description: str = ""
    conflict_thread: str = ""
    open_question: str = ""
    missing_context: List[str] = Field(default_factory=list)
    est_duration_s: float = 0.0
    confidence: float = 0.5
    link: str = ""
    structure: str = "continuous"               # continuous | jump_cut | seam_bridge | contrast | flash_forward
    flash_forward: bool = False
    base_id: str = ""                           # flash-forward variants point at their base promo
    # engine output
    segments: List[Dict[str, Any]] = Field(default_factory=list)      # CutSegment.as_dict()
    duration_s: float = 0.0
    hard: List[str] = Field(default_factory=list)
    soft: List[str] = Field(default_factory=list)
    dead_air_s: float = 0.0
    cut_info: Dict[str, Any] = Field(default_factory=dict)
    repairs: int = 0
    status: str = "candidate"                   # candidate | cut_ok | dropped
    drop_reason: str = ""
    family_id: str = ""
    merged_from: List[str] = Field(default_factory=list)
    # judging
    signals: Dict[str, float] = Field(default_factory=dict)           # cliff_signal, opening_arousal, ...
    scores: Dict[str, Any] = Field(default_factory=dict)              # hook, cliff_pull, composite, pl ...
    flags: List[str] = Field(default_factory=list)
    judge_models: Dict[str, str] = Field(default_factory=dict)
    editorial: Dict[str, Any] = Field(default_factory=dict)

    @property
    def entry_ep(self) -> int:
        return int(self.segments[0]["ep"]) if self.segments else 0
